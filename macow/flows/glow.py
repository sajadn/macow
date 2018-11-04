__author__ = 'max'

import warnings
from overrides import overrides
from typing import Dict, Tuple
import torch
import torch.nn as nn

from macow.flows.flow import Flow
from macow.flows.conv import Conv1x1Flow
from macow.flows.nice import NICE
from macow.utils import squeeze2d, unsqueeze2d, split2d, unsplit2d


class GlowStep(Flow):
    """
    A step of Glow. A Conv1x1 followed with a NICE
    """
    def __init__(self, in_channels, hidden_channels=None, scale=True, inverse=False):
        super(GlowStep, self).__init__(inverse)
        self.conv1x1 = Conv1x1Flow(in_channels, inverse=inverse)
        self.coupling = NICE(in_channels, hidden_channels=hidden_channels, scale=scale, inverse=inverse)

    @overrides
    def forward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        out, logdet_accum = self.conv1x1.forward(input, h=h)
        out, logdet = self.coupling.forward(out, h=h)
        logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    def backward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        out, logdet_accum = self.coupling.backward(input)
        out, logdet = self.conv1x1.backward(out, h=h)
        logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    @overrides
    def init(self, data, h=None, init_scale=1.0) -> Tuple[torch.Tensor, torch.Tensor]:
        out, logdet_accum = self.conv1x1.init(data, h=h, init_scale=init_scale)
        out, logdet = self.coupling.init(out, h=h, init_scale=init_scale)
        logdet_accum = logdet_accum + logdet
        return out, logdet_accum


class GlowTopBlock(Flow):
    """
    Glow Block (squeeze at beginning)
    """
    def __init__(self, num_steps, in_channels, scale=True, inverse=False):
        super(GlowTopBlock, self).__init__(inverse)
        steps = [GlowStep(in_channels, scale=scale, inverse=inverse) for _ in range(num_steps)]
        self.steps = nn.ModuleList(steps)

    @overrides
    def forward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        out = input
        # [batch]
        logdet_accum = input.new_zeros(input.size(0))
        for step in self.steps:
            out, logdet = step.forward(out, h=h)
            logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    @overrides
    def backward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        logdet_accum = input.new_zeros(input.size(0))
        out = input
        for step in reversed(self.steps):
            out, logdet = step.backward(out, h=h)
            logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    @overrides
    def init(self, data, h=None, init_scale=1.0) -> Tuple[torch.Tensor, torch.Tensor]:
        out = data
        # [batch]
        logdet_accum = data.new_zeros(data.size(0))
        for step in self.steps:
            out, logdet = step.init(out, h=h, init_scale=init_scale)
            logdet_accum = logdet_accum + logdet
        return out, logdet_accum


class GlowInternalBlock(Flow):
    """
    Glow Internal Block (squeeze at beginning and split at end)
    """
    def __init__(self, num_steps, in_channels, scale=True, inverse=False):
        super(GlowInternalBlock, self).__init__(inverse)
        steps = [GlowStep(in_channels, scale=scale, inverse=inverse) for _ in range(num_steps)]
        self.steps = nn.ModuleList(steps)
        self.prior = NICE(in_channels, scale=True, inverse=inverse)

    @overrides
    def forward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        out = input
        # [batch]
        logdet_accum = input.new_zeros(input.size(0))
        for step in self.steps:
            out, logdet = step.forward(out, h=h)
            logdet_accum = logdet_accum + logdet
        out, logdet = self.prior.forward(out, h=h)
        logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    @overrides
    def backward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        # [batch]
        out, logdet_accum = self.prior.backward(input, h=h)
        for step in reversed(self.steps):
            out, logdet = step.backward(out, h=h)
            logdet_accum = logdet_accum + logdet
        return out, logdet_accum

    @overrides
    def init(self, data, h=None, init_scale=1.0) -> Tuple[Tuple[torch.Tensor, torch.Tensor], torch.Tensor]:
        out = data
        # [batch]
        logdet_accum = data.new_zeros(data.size(0))
        for step in self.steps:
            out, logdet = step.init(out, h=h, init_scale=init_scale)
            logdet_accum = logdet_accum + logdet
        out, logdet = self.prior.init(out, h=h, init_scale=init_scale)
        logdet_accum = logdet_accum + logdet
        return out, logdet_accum


class Glow(Flow):
    """
    Glow
    """
    def __init__(self, levels, num_steps, in_channels, scale=True, inverse=False):
        super(Glow, self).__init__(inverse)
        assert levels > 1, 'Glow should have at least 2 levels.'
        assert levels == len(num_steps)
        blocks = []
        self.levels = levels
        for level in range(levels):
            if level == levels - 1:
                in_channels = in_channels * 4
                macow_block = GlowTopBlock(num_steps[level], in_channels, scale=scale, inverse=inverse)
                blocks.append(macow_block)
            else:
                in_channels = in_channels * 4
                macow_block = GlowInternalBlock(num_steps[level], in_channels, scale=scale, inverse=inverse)
                blocks.append(macow_block)
                in_channels = in_channels // 2
        self.blocks = nn.ModuleList(blocks)

    @overrides
    def forward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        logdet_accum = input.new_zeros(input.size(0))
        out = input
        outputs = []
        for i, block in enumerate(self.blocks):
            out = squeeze2d(out, factor=2)
            out, logdet = block.forward(out, h=h)
            logdet_accum = logdet_accum + logdet
            if isinstance(block, GlowInternalBlock):
                out1, out2 = split2d(out)
                outputs.append(out2)
                out = out1

        out = unsqueeze2d(out, factor=2)
        for _ in range(self.levels - 1):
            out2 = outputs.pop()
            out = unsqueeze2d(unsplit2d(out, out2), factor=2)
        assert len(outputs) == 0
        return out, logdet_accum

    @overrides
    def backward(self, input: torch.Tensor, h=None) -> Tuple[torch.Tensor, torch.Tensor]:
        outputs = []
        out = squeeze2d(input, factor=2)
        for _ in range(self.levels - 1):
            out1, out2 = split2d(out)
            outputs.append(out2)
            out = squeeze2d(out1, factor=2)

        logdet_accum = input.new_zeros(input.size(0))
        for i, block in enumerate(reversed(self.blocks)):
            if isinstance(block, GlowInternalBlock):
                out2 = outputs.pop()
                out = unsplit2d(out, out2)
            out, logdet = block.backward(out, h=h)
            logdet_accum = logdet_accum + logdet
            out = unsqueeze2d(out, factor=2)
        assert len(outputs) == 0
        return out, logdet_accum

    @overrides
    def init(self, data, h=None, init_scale=1.0) -> Tuple[torch.Tensor, torch.Tensor]:
        logdet_accum = data.new_zeros(data.size(0))
        out = data
        outputs = []
        for i, block in enumerate(self.blocks):
            out = squeeze2d(out, factor=2)
            out, logdet = block.init(out, h=h, init_scale=init_scale)
            logdet_accum = logdet_accum + logdet
            if isinstance(block, GlowInternalBlock):
                out1, out2 = split2d(out)
                outputs.append(out2)
                out = out1

        out = unsqueeze2d(out, factor=2)
        for _ in range(self.levels - 1):
            out2 = outputs.pop()
            out = unsqueeze2d(unsplit2d(out, out2), factor=2)
        assert len(outputs) == 0
        return out, logdet_accum

    @classmethod
    def from_params(cls, params: Dict) -> "Glow":
        return Glow(**params)


Glow.register('glow')