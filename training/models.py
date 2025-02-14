#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jan 14 12:11:43 2022

@author: matthijs
"""
import torch
from torch_geometric.nn import SAGEConv, Linear, HeteroConv
from typing import Dict, List
from enum import Enum, unique


class GCN(torch.nn.Module):
    """
    Graph convolutional network model.
    Consists of: two embedding layers that should embed the different object
    features into a common embedding; and a number of GCN layers.
    """

    @unique
    class NetworkType(Enum):
        HETERO = 'heterogeneous'
        HOMO = 'homogeneous'

    def __init__(self,
                 LReLu_neg_slope: float,
                 weight_init_std: float,
                 N_f_gen: int,
                 N_f_load: int,
                 N_f_endpoint: int,
                 N_GCN_layers: int,
                 N_node_hidden: int,
                 aggr: str,
                 network_type: NetworkType) -> object:
        """
        Parameters
        ----------
        LReLu_neg_slope : float
            The negative slope of the LReLu activation function.
        weight_init_std: float,
            The standard deviation of the normal distribution according to
            which the weights are initialized.
        N_f_gen : int
            The number of features per generator object.
        N_f_load : int
            The number of features per load object.
        N_f_endpoint : int
            The number of features per endpoint object.
        N_GCN_layers : int
            The number of GCN layers.
        N_node_hidden : int
            The number of hidden nodes in the hidden layers.
        aggr : str
            The aggregation function for GCN layers. Should be 'add' or 'mean'.
        network_type : NetworkType
            The type of network.
        """
        super().__init__()
        self.network_type = network_type
        HOMO = GCN.NetworkType.HOMO
        HETERO = GCN.NetworkType.HETERO

        # The activation function. Inplace helps make it work for both
        # network types.
        self.LReLu_neg_slope = LReLu_neg_slope
        self.activation_f = torch.nn.LeakyReLU(LReLu_neg_slope, inplace=True)

        # The embedding layers

        self.lin_gen_1 = Linear(N_f_gen, N_node_hidden)
        self.lin_gen_2 = Linear(N_node_hidden, N_node_hidden)

        self.lin_load_1 = Linear(N_f_load, N_node_hidden)
        self.lin_load_2 = Linear(N_node_hidden, N_node_hidden)

        self.lin_or_1 = Linear(N_f_endpoint, N_node_hidden)
        self.lin_or_2 = Linear(N_node_hidden, N_node_hidden)

        self.lin_ex_1 = Linear(N_f_endpoint, N_node_hidden)
        self.lin_ex_2 = Linear(N_node_hidden, N_node_hidden)

        # Factory function that creates GCN layers.
        def GCN_layer(n_in, n_out, aggr_f='add'):
            if network_type == HOMO:
                return SAGEConv(n_in, n_out, root_weight=True, aggr=aggr_f)
            elif network_type == HETERO:
                return HeteroConv({
                    ('object', 'line', 'object'):
                        SAGEConv(n_in, n_out, root_weight=False, aggr=aggr_f, bias=False),
                    ('object', 'same_busbar', 'object'):
                        SAGEConv(n_in, n_out, root_weight=True, aggr=aggr_f, bias=True),
                    ('object', 'other_busbar', 'object'):
                        SAGEConv(n_in, n_out, root_weight=False, aggr=aggr_f, bias=False),
                }, aggr='sum' if aggr_f == 'add' else aggr_f)

        # Create the GCN layers
        self.GCN_layers = torch.nn.ModuleList([GCN_layer(N_node_hidden, N_node_hidden, aggr)
                                               for _ in range(N_GCN_layers - 1)])
        # Create the final layer
        self.GCN_layers.append(GCN_layer(N_node_hidden, 1, aggr))

        # Initialize weights according to normal distribution
        self.init_weights_normalized_normal(weight_init_std)

    def forward(self,
                x_gen: torch.Tensor,
                x_load: torch.Tensor,
                x_or: torch.Tensor,
                x_ex: torch.Tensor,
                edge_index: torch.Tensor,
                object_ptv: torch.Tensor) -> torch.Tensor:
        """
        Passes the datapoint through the network.

        Parameters
        ----------
        x_gen : torch.Tensor
            The generator features. Columns represent features, rows different
            generator objects.
        x_load : torch.Tensor
            The load features. Columns represent features, rows different
            load objects.
        x_or : torch.Tensor
            The line origin features. Columns represent features, rows different
            line origin objects.
        x_ex : torch.Tensor
            The line extremity features. Columns represent features, rows different
            line extremity objects.
        edge_index : torch.Tensor
            The edge indices of the adjacency matrix. Edge indices indicate
            the indices of objects in the topoly vector.
            Should have shape (2,num_edges). Should be symmetric.
        object_ptv : torch.Tensor
            Vector describing the permutation required to set the different object
            at their position in the topology vector.

        Returns
        -------
        x : torch.Tensor
            The output vector. Values should be in range (0,1).
        """
        HOMO = GCN.NetworkType.HOMO
        HETERO = GCN.NetworkType.HETERO

        # Passing the object features through their respective
        # embedding layers
        x_gen = self.lin_gen_1(x_gen)
        self.activation_f(x_gen)
        x_gen = self.lin_gen_2(x_gen)
        self.activation_f(x_gen)
        x_load = self.lin_load_1(x_load)
        self.activation_f(x_load)
        x_load = self.lin_load_2(x_load)
        self.activation_f(x_load)
        x_or = self.lin_or_1(x_or)
        self.activation_f(x_or)
        x_or = self.lin_or_2(x_or)
        self.activation_f(x_or)
        x_ex = self.lin_ex_1(x_ex)
        self.activation_f(x_ex)
        x_ex = self.lin_ex_2(x_ex)
        self.activation_f(x_ex)

        # Combining different object states into the order of the
        # topology vector
        x = torch.cat([x_gen, x_load, x_or, x_ex], axis=0)
        x = x[object_ptv]

        if self.network_type == HETERO:
            x = {'object': x}

        # Pass through the GCN layers
        for l in self.GCN_layers[:-1]:
            x = l(x, edge_index)
            self.activation_f(x if self.network_type == HOMO else x['object'])

        # Pass through the final layer and the sigmoid activation function
        x = self.GCN_layers[-1](x, edge_index)
        x = torch.sigmoid(x if self.network_type == HOMO else x['object'])

        return x

    def init_weights_kaiming(self):
        """
        Initialize the weights of this network according to the kaiming uniform
        distribution. The biases are initialized to zero.
        """

        def layer_weights_kaiming(m):
            """
            Apply kaiming initialization to a single layer.
            """
            classname = m.__class__.__name__
            # for every Linear layer in a model.
            if classname.find('Linear') != -1:
                torch.nn.init.kaiming_normal_(m.weight, a=self.LReLu_neg_slope)
                if m.bias is not None:
                    m.bias.data.fill_(0)

        self.apply(layer_weights_kaiming)

    def init_weights_normalized_normal(self, weight_init_std: float):
        """
        Initialize the weights of this network according to the normal
        distribution, but with the std divided by the number of in channels.
        The biases are initialized to zero.

        Parameters
        ----------
        weight_init_std : float
            The standard deviation of the normal distribution.
        """

        def layer_weights_normal(m):
            """
            Apply initialization to a single layer.
            """
            classname = m.__class__.__name__
            # for every Linear layer in a model.
            if classname.find('Linear') != -1:
                std = weight_init_std / m.in_channels
                torch.nn.init.normal_(m.weight, std=std)
                if m.bias is not None:
                    m.bias.data.fill_(0)

        self.apply(layer_weights_normal)

    def compute_difference_weights(self) -> Dict[str, List[float]]:
        """
        Compute the difference between the self weights and the neighbour
        weights.

        Returns
        -------
        diffs : Dict[str,List[float]]
            The dictionary  of lists (each lists corresponding to one neighbour
            weight type) with differences (each difference corresponding to a
            one layer). The number of lists depends on the network type.
        """

        def l_w_norm(layer):
            """
            Calculate the norm of the weights of a layer.
            """
            return abs(layer.weight).sum().item()

        diffs = {}
        if self.network_type == GCN.NetworkType.HETERO:

            diffs['self_line_neigh'] = []
            diffs['self_sb_neigh'] = []
            diffs['self_ob_neigh'] = []

            for l in self.GCN_layers:
                l_key = 'object__line__object'
                sb_key = 'object__same_busbar__object'
                ob_key = 'object__other_busbar__object'

                norm_w_self = l_w_norm(l.convs[sb_key].lin_r)
                norm_w_line = l_w_norm(l.convs[l_key].lin_l)
                norm_w_sb = l_w_norm(l.convs[sb_key].lin_l)
                norm_w_ob = l_w_norm(l.convs[ob_key].lin_l)

                diffs['self_line_neigh'].append(norm_w_self - norm_w_line)
                diffs['self_sb_neigh'].append(norm_w_self - norm_w_sb)
                diffs['self_ob_neigh'].append(norm_w_self - norm_w_ob)

        elif self.network_type == GCN.NetworkType.HOMO:

            diffs['self_neigh'] = []

            for l in self.GCN_layers:
                norm_w_self = l_w_norm(l.lin_r)
                norm_w_neigh = l_w_norm(l.lin_l)
                diffs['self_neigh'].append(norm_w_self - norm_w_neigh)

        return diffs


class FCNN(torch.nn.Module):
    """
    Fully connected neural network. Consists of multiple feedforward layers.
    """

    def __init__(self,
                 LReLu_neg_slope: float,
                 weight_init_std: float,
                 size_in: int,
                 size_out: int,
                 N_layers: int,
                 N_node_hidden: int):
        """
        Parameters
        ----------
        LReLu_neg_slope : float
            The negative slope of the LReLu activation function.
        weight_init_std: float,
            The standard deviation of the normal distribution according to which the weights are initialized.
        N_layers : int
            The number of layers.
        N_node_hidden : int
            The number of nodes in the hidden layers.
        """
        super().__init__()

        # The activation function.
        self.LReLu_neg_slope = LReLu_neg_slope
        self.activation_f = torch.nn.LeakyReLU(LReLu_neg_slope)

        # The first layer
        self.lin_first = Linear(size_in, N_node_hidden)

        # Create the middle layers
        self.lin_middle_layers = torch.nn.ModuleList([Linear(N_node_hidden, N_node_hidden)
                                                      for _ in range(N_layers - 2)])

        # The last layer
        self.lin_last = Linear(N_node_hidden, size_out)

        # Initialize weights according to normal distribution
        self.init_weights_normalized_normal(weight_init_std)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Passes the datapoint through the network.

        Parameters
        ----------
        x : torch.Tensor
            The input of the model (i.e. the features).
        Returns
        -------
        x : torch.Tensor
            The output vector. Values should be in range (0,1).
        """
        # Passing the states through the consecutive linear (i.e. fully connected) layers
        x = self.lin_first(x)
        x = self.activation_f(x)

        for l in self.lin_middle_layers:
            x = l(x)
            x = self.activation_f(x)

        x = self.lin_last(x)
        x = torch.sigmoid(x)

        return x

    def init_weights_normalized_normal(self, weight_init_std: float):
        """
        Initialize the weights of this network according to the normal
        distribution, but with the std divided by the number of in channels.
        The biases are initialized to zero.

        Parameters
        ----------
        weight_init_std : float
            The standard deviation of the normal distribution.
        """

        def layer_weights_normal(m):
            """
            Apply initialization to a single layer.
            """
            classname = m.__class__.__name__
            # for every Linear layer in a model.
            if classname.find('Linear') != -1:
                std = weight_init_std / m.in_channels
                torch.nn.init.normal_(m.weight, std=std)
                if m.bias is not None:
                    m.bias.data.fill_(0)

        self.apply(layer_weights_normal)
