import numpy as np
from math import sqrt

from cppn import CPPN, Sampler

import torch
import torch.nn as nn
import torch.nn.functional as F


def totalistic(x, dim2=False):
    if dim2:
        y_idx = 0
        x_idx = 1
    else:
        y_idx = 2
        x_idx = 3
    z = 0.125 * (x + x.flip(y_idx) + x.flip(x_idx) + x.flip(y_idx).flip(x_idx))
    z = z + 0.125 * (x.transpose(y_idx, x_idx) +
                     x.transpose(y_idx, x_idx).flip(y_idx) +
                     x.transpose(y_idx, x_idx).flip(x_idx) +
                     x.transpose(y_idx, x_idx).flip(y_idx).flip(x_idx))
    # if dim2:
    #     z = z - z.mean()
    # else:
    #     z = z - z.mean(x_idx).mean(y_idx).unsqueeze(y_idx).unsqueeze(x_idx)

    return z


class Rule(nn.Module):
    def __init__(self, CHANNELS=8, FILTERS=1, NET_SIZE=[32, 32, 32], RADIUS=2):
        super().__init__()
        self.channels = CHANNELS
        self.filters = FILTERS
        self.radius = RADIUS
        net_size = [1] + NET_SIZE + [CHANNELS]

        self.modules = nn.ModuleList()

        # toggle options
        self.totalistic = True

        ###########################################
        # init CPPN to generate kernels

        cppn_net_size = [32, 32, 32]
        dim_z = 16
        dim_c = 1 #CHANNELS
        self.cppn = CPPN(cppn_net_size, dim_z, dim_c).cuda().eval()
        self.sampler = Sampler()

        self.generate_kernel(self.cppn)

        # for each neighbourhood, generate a transition function (sequence)
        self.transitions = nn.ModuleList()
        for j in range(FILTERS):
            layers = []
            for i in range(len(net_size[:-1])):
                if (i + 1) < len(net_size):
                    activation = True
                else:
                    activation = False
                layers.append(MNCA_block(net_size[i], net_size[i + 1], radius=1, activation=activation))

            seq = nn.Sequential(*layers)
            self.transitions.append(seq)

    def generate_kernel(self, cppn):
        Rk = self.radius * 2 + 1
        dim_z = cppn.dim_z
        dim_c = cppn.dim_c

        coords = self.generate_coords(dim_z, Rk, zscale=2, scale=5)

        xm, ym = torch.meshgrid(torch.linspace(-1, 1, Rk), torch.linspace(-1, 1, Rk))
        rm = torch.sqrt(xm ** 2 + ym ** 2).cuda().unsqueeze(0).unsqueeze(0)
        null = torch.zeros_like(rm).cuda()
        #condition = (rm < 0.9) & (rm > 0.2)
        condition = (rm < 0.9)

        kernels = []
        for i in range(self.filters):
            ks = []
            coords[-1] = 2*torch.randn(1, dim_z).cuda()
            # k = (self.cppn.forward(coords, Rk, Rk).reshape(1, Rk, Rk, dim_c).permute(0, 3, 1, 2) > 0.9).type(torch.cuda.FloatTensor)
            k = (self.cppn.forward(coords, Rk, Rk).reshape(1, Rk, Rk, dim_c).permute(0, 3, 1, 2))
            k = torch.where(condition, k, null)
            k[condition] -= k[condition].sum() / k[condition].numel()  # subtract inner-circle mean from inner-circle
            k = k.repeat((1, self.channels, 1, 1))

            # for i_k in range(self.channels):
            #     k_ik = k[:, [i_k], ...]
            #     k_ik = torch.where(condition, k_ik, null)
            #     k_ik[condition] -= k_ik[condition].sum() / k_ik[condition].numel()  # subtract inner-circle mean from inner-circle
            #     k[:, i_k, ...] = k_ik
            #
            # k = k - k.mean()

            kernels.append(nn.Parameter(k))
        # kernels = [(torch.randn(1, CHANNELS, Rk, Rk) > 0).type(torch.FloatTensor) for i in range(FILTERS)]
        self.kernels = nn.ParameterList([nn.Parameter(k) for k in kernels])
        self.bias = nn.ParameterList([nn.Parameter(0 * torch.randn(1)) for i in range(self.filters)])

    def generate_coords(self, dim_z, Rk, zscale=1, scale=5):

        z = zscale * torch.randn(1, dim_z).cuda()
        coords = self.cppn._coordinates(scale, Rk, Rk, z)

        coords[0] = 10 + coords[2] * 2
        coords[1] = 10 + coords[2] / 2
        coords[2] = 10 + 5 * coords[2]

        # coords[0] = coords[2] * 2
        # coords[1] = torch.cos(coords[2] / 2)
        # coords[2] = torch.sin(5 * coords[2] * coords[2])

        return coords
        ###########################################


class MNCA_block(nn.Module):

    def __init__(self, in_channels, out_channels, radius, activation=True):
        super().__init__()
        self.PrecisionValue = torch.floor(torch.tensor([2 ** 31 / (128)])).cuda()
        self.activation = activation
        self.conv = nn.Conv2d(in_channels, out_channels, 2*radius+1, padding_mode='circular', padding=radius, bias=False)
        nn.init.orthogonal_(self.conv.weight)
        # nn.init.normal_(layers[-1].weight)
        # nn.init.sparse_(layers[-1].weight, sparsity=0.9, std=1)
        self.afunc = nn.Tanh()
        # self.afunc = nn.ReLU()

    def forward(self, x):
        x = torch.floor(x * self.PrecisionValue)
        x = self.conv(x)
        x = torch.floor(x) / self.PrecisionValue
        if self.activation:
            x = self.afunc(x)

        return x

class CA(nn.Module):
    def __init__(self, CHANNELS=8, FILTERS=1, NET_SIZE=[32, 32, 32], RADIUS=2):
        super().__init__()
        self.channels = CHANNELS
        self.filters = FILTERS
        self.radius = RADIUS

        Rk = 2 * RADIUS + 1
        self.PrecisionValue = torch.floor(torch.tensor([2 ** 31 / (Rk * Rk * 128)])).cuda()
        self.rule = Rule(CHANNELS, FILTERS, NET_SIZE, RADIUS)
        # self.optim = torch.optim.Adam(self.parameters(), lr=1e-3)

    def initGrid(self, BS, RES):
        self.psi = torch.cuda.FloatTensor(2 * np.random.rand(BS, self.channels, RES, RES) - 1)

    def seed(self, RES, n):
        seed = torch.FloatTensor(np.zeros((n, self.channels, RES, RES)))
        # seed[:, 3:, RES // 2, RES // 2] = 1
        return seed

    def get_living_mask(self, x, alive_thres=0, dead_thres=0.6):
        alpha_channel = x[:, 3:4, :, :]
        R = self.radius // 2
        alpha_channel = F.pad(alpha_channel, (R, R, R, R), mode='circular')

        alive_mask = F.max_pool2d(alpha_channel, kernel_size=2 * R + 1, stride=1, padding=R) > alive_thres
        alive_mask = alive_mask[:, :, R:-R, R:-R]

        # death_mask = F.avg_pool2d(alpha_channel, kernel_size=2 * R + 1, stride=1, padding=R) < dead_thres
        # death_mask = death_mask[:, :, R:-R, R:-R]
        # return alive_mask & death_mask

        return alive_mask

    def forward(self, x, update_rate=1):
        # circular/gaussian kernel mask
        kernels = self.rule.kernels
        if self.rule.totalistic:
            kernels = [totalistic(k) for k in kernels]

        bias = [b for b in self.rule.bias]
        R = self.radius

        z = torch.floor(x * self.PrecisionValue)

        z = F.pad(z, (R, R, R, R), 'circular')

        perceptions = [F.conv2d(z, weight=kernels[i], bias=bias[i], padding=0) for i in range(len(kernels))]
        perceptions = [torch.floor(p) / self.PrecisionValue for i, p in enumerate(perceptions)]
        out = []
        for i, p in enumerate(perceptions):
            out.append(self.rule.transitions[i](p))
        z = torch.stack(out)


        # min_idx = torch.argmin(z, dim=0, keepdim=True)
        # z = torch.gather(z, 0, min_idx)[0]

        idx = torch.argsort(z, dim=0)
        z = torch.gather(z, 0, idx)[self.filters//2]

        # z = torch.stack(out).mean(0) * update_rate

        # for i in range(len(kernels)):
        #     z = F.pad(x, (R, R, R, R), 'circular')
        #     z = F.conv2d(z, weight=kernels[i], bias=bias[i], padding=0)
        #     z = self.rule.transitions[i](z)

        lifemask = self.get_living_mask(x, alive_thres=.1, dead_thres=0.6)
        x = x + (lifemask * z * update_rate)
        # deathR = 2*R
        # x = z - F.avg_pool2d(F.pad(z, (deathR, deathR, deathR, deathR), 'circular'), 2 * deathR + 1, padding=0, stride=1) * update_rate
        # z = x + (z - z.mean(dim=2, keepdim=True).mean(dim=3, keepdim=True))* update_rate
        # z = x + z * update_rate
        # x = torch.clamp(z, 0, 255)
        # x = torch.clamp(z, -127, 128)
        x = torch.clamp(x, 0, 1)
        # x = z
        return x

    def cleanup(self):
        del self.psi

class SwapC_with_Last(torch.nn.Module):
    def forward(self, x):
        return x.permute(0, 3, 2, 1)