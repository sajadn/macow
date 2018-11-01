__author__ = 'max'

import os
import json
import math
from typing import Dict, Tuple, List
import torch
import torch.nn as nn

from macow.flows.flow import Flow
from macow.flows.parallel import DataParallelFlow


class FlowGenModel(nn.Module):
    """
    Flow-based Generative model
    """
    def __init__(self, flow: Flow, ngpu=1):
        super(FlowGenModel, self).__init__()
        assert flow.inverse, 'flow based generative should have inverse mode'
        self.flow = flow
        assert ngpu > 0, 'the number of GPUs should be positive.'
        self.ngpu = ngpu
        if ngpu > 1:
            self.flow = DataParallelFlow(self.flow)

    def encode(self, x) -> Tuple[torch.Tensor, torch.Tensor, List[torch.Tensor]]:
        """

        Args:
            x: Tensor
                The input data with shape =[batch, x_shape]

        Returns: z: Tensor, logdet: Tensor, eps: List[Tensor]
            z, the latent variable
            logdet, the log determinant of :math:`\partial z / \partial x`
            Then the density :math:`\log(p(x)) = \log(p(z)) + logdet`
            eps: eps for multi-scale architecture.
        """
        z, logdet, eps = self.flow.bwdpass(x)
        return z, logdet, eps

    def decode(self, z, eps=None) -> Tuple[torch.Tensor, torch.Tensor]:
        """

        Args:
            z: Tensor
                The latent code with shape =[batch, *]

        Returns: x: Tensor, logdet: Tensor
            x, the decoded variable
            logdet, the log determinant of :math:`\partial z / \partial x`
            Then the density :math:`\log(p(x)) = \log(p(z)) + logdet`
        """
        x, logdet = self.flow.fwdpass(z, eps=eps)
        return x, logdet

    def init(self, data, init_scale=1.0) -> Tuple[torch.Tensor, torch.Tensor, List[torch.Tensor]]:
        return self.flow.bwdpass(data, init=True, init_scale=init_scale)

    def log_probability(self, x) -> torch.Tensor:
        """

        Args:
            x: Tensor
                The input data with shape =[batch, x_shape]

        Returns:
            Tensor
            The tensor of the posterior probabilities of x shape = [batch]
        """
        # [batch, x_shape]
        z, logdet, eps = self.encode(x)
        log_probs = z.pow(2) + math.log(math.pi * 2.)
        # [batch, x_shape] --> [batch, numels] -- > [batch]
        log_probs = log_probs.view(z.size(0), -1).sum(dim=1) * -0.5 + logdet
        return log_probs

    @classmethod
    def from_params(cls, params: Dict) -> "FlowGenModel":
        flow_params = params.pop('flow')
        flow = Flow.by_name(flow_params.pop('type')).from_params(flow_params)
        return FlowGenModel(flow, **params)

    @classmethod
    def load(cls, model_path, device) -> "FlowGenModel":
        params = json.load(open(os.path.join(model_path, 'config.json'), 'r'))
        model_name = os.path.join(model_path, 'model.pt')
        fgen = FlowGenModel.from_params(params)
        fgen.load_state_dict(torch.load(model_name, map_location=device))
        return fgen.to(device)
