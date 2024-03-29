import numpy as np
from math import sqrt

from cppn import CPPN, Sampler

import torch
import torch.nn as nn
import torch.nn.functional as F


class Rule(nn.Module):
    def __init__(self, RADIUS=2):
        super().__init__()
        self.radius = RADIUS

        self.integration_constant = 20.
        self.decay_constant = 0.2

        # self.threshhold = 0.68
        self.minimum_threshold = 0.1
        self.refractor_time = 5
        self.target_rate = 5

        self.energy_recovery = 0.0058
        self.target_energy = 1
        self.spike_cost = 0.1
        self.min_energy = 0.1

        self.excite_prob = 0.8

        Rk = 2*RADIUS + 1

        ########## CPPN KERNEL ################
        cppn_net_size = [32, 32, 32]
        dim_z = 16
        dim_c = 1  # CHANNELS
        self.cppn = CPPN(cppn_net_size, dim_z, dim_c).cuda().eval()
        self.sampler = Sampler()

        k = self.generate_cppn_kernel()

        # radial kernel
        xm, ym = torch.meshgrid(torch.linspace(-1, 1, Rk), torch.linspace(-1, 1, Rk))
        rm = torch.sqrt(xm ** 2 + ym ** 2).cuda()
        condition = rm < 1.
        null = torch.zeros_like(rm).cuda()
        k = torch.where(condition, k, null)

        # sparsity
        spars_mat = (torch.rand_like(k) > 0.0) * 1.
        self.nearest_neighbours = k * spars_mat
        ###########################################

        ########## NEAREST NEIGHBOUr KERNEL ################
        # nearest_neighbours = torch.ones(1, 1, Rk, Rk).cuda()
        # nearest_neighbours[:, :, RADIUS, RADIUS] = 0
        # nearest_neighbours = nearest_neighbours * 0.1
        # nearest_neighbours = nearest_neighbours * (torch.rand_like(nearest_neighbours) > 0.9)
        #
        # self.nearest_neighbours = nearest_neighbours
        ###########################################




    def generate_cppn_kernel(self):
        scale = 5
        zscale = 2
        z = zscale * torch.randn(1, self.cppn.dim_z).cuda()
        Rk = self.radius * 2 + 1
        coords = self.cppn._coordinates(scale, Rk, Rk, z)
        coords[0] = 10 + coords[2]
        coords[1] = 10 + coords[2]

        k = self.cppn.forward(coords, Rk, Rk).reshape(1, Rk, Rk, self.cppn.dim_c).permute(0, 3, 1, 2)
        k = k / k.sum().sqrt()
        return k

    def forward(self, x, noise_input):

        Rk = self.radius

        # pre = F.unfold(x[:, [0], ...], 1)

        x[:, [0], ...] = x[:, [0], ...] * self.EI
        S = F.pad(x[:, [0], ...], (Rk, Rk, Rk, Rk), mode='circular') #spikes
        V = x[:, [1], ...] # voltages
        R = x[:, [2], ...] # + ((torch.rand_like(V) > 0.5) * 2. - 1.) # refractory time
        A = x[:, [3], ...] # traces
        T = x[:, [4], ...] # threshold
        E = x[:, [5], ...] # energy

        # calculate new spikes
        V = V + noise_input #
        I = F.conv2d(S, self.nearest_neighbours, padding=0)
        V = V + self.decay_constant * (-V + self.integration_constant * I)
        S = (V > T) * (R > self.refractor_time) * (E > self.min_energy) * 1.

        # post = F.pad(S, (Rk, Rk, Rk, Rk), mode='circular') #pre
        # post = F.unfold(post, 2*Rk + 1)
        # delta = (pre * post).mean(dim=2).reshape(1, 1, 2*Rk+1, 2*Rk+1)
        # delta -= delta.mean()
        # new_k = torch.clip(self.nearest_neighbours + 0.1*delta, min=0)

        # self.nearest_neighbours = new_k / (new_k.sum() + 1e-6) * self.nearest_neighbours.sum()

        # update cell properties
        E = E + self.energy_recovery * (self.target_energy - E) - (S * self.spike_cost)
        R = (R + 1) * (1 - S)
        A = A - A/100 + S
        T = T + 0.05 * (A - self.target_rate_mat)
        T = torch.clamp(T, min=self.minimum_threshold)
        V = V * (1 - S)

        z = torch.cat([S, V, R, A, T, E, self.EI], axis=1)

        return z

class iafCA(nn.Module):
    def __init__(self, RADIUS=2):
        super().__init__()
        self.radius = RADIUS

        self.rule = Rule(RADIUS)


    def initGrid(self, shape):
        rand = torch.rand(1, 6, shape[0], shape[1]) * 2.

        xm, ym = torch.meshgrid(torch.linspace(-1, 1, shape[0]), shape[1] / shape[0] * torch.linspace(-1, 1, shape[1]))
        self.rm = torch.sqrt(xm ** 2 + ym ** 2).cuda()

        self.rule.EI = ((torch.rand(1, 1, shape[0], shape[1]) < self.rule.excite_prob) * 2. - 1).cuda()
        self.rule.EI = torch.where(self.rule.EI < 0., self.rule.EI * (1/self.rule.excite_prob), self.rule.EI)
        self.rule.target_rate_mat = self.rule.target_rate * torch.ones_like(self.rule.EI)
        return torch.cat([rand.cuda(), self.rule.EI], axis=1)

    def forward(self, x):
        # noise_input = (torch.rand_like(x[:, [1], ...]) > 0.9) * (self.rm > 0.5) * (self.rm - self.rm.mean())
        noise_input = (torch.randn_like(x[:, [1], ...]) > 0.99) * 1.
        # noise_input = 0.
        return self.rule(x, noise_input)
