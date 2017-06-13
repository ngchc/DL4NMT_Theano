#! /usr/bin/python
# -*- coding: utf-8 -*-

from collections import OrderedDict

import numpy as np
import theano
from theano import tensor as T

from constants import fX, profile
from utils import _p, normal_weight, orthogonal_weight, concatenate

__author__ = 'fyabc'


# Some utilities.

def _slice(_x, n, dim):
    """Utility function to slice a tensor."""

    if _x.ndim == 3:
        return _x[:, :, n * dim:(n + 1) * dim]
    return _x[:, n * dim:(n + 1) * dim]


# Activations.

def tanh(x):
    return T.tanh(x)


def linear(x):
    return x


# Some helper layers.

def dropout_layer(state_before, use_noise, trng, dropout_rate=0.5):
    """Dropout"""

    projection = T.switch(
        use_noise,
        state_before * trng.binomial(state_before.shape, p=(1. - dropout_rate), n=1,
                                     dtype=state_before.dtype),
        state_before * (1. - dropout_rate))
    return projection


def attention_layer(context_mask, et, ht_1, We_att, Wh_att, Wb_att, U_att, Ub_att):
    """Attention"""

    a_network = T.tanh(T.dot(et, We_att) + T.dot(ht_1, Wh_att) + Wb_att)
    alpha = T.dot(a_network, U_att) + Ub_att
    alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
    alpha = T.exp(alpha)
    if context_mask:
        alpha *= context_mask
    alpha = alpha / alpha.sum(0, keepdims=True)
    # if Wp_compress_e:
    #    ctx_t = (tensor.dot(et, Wp_compress_e) * alpha[:,:,None]).sum(0) # This is the c_t in Baidu's paper
    # else:
    #    ctx_t = (et * alpha[:,:,None]).sum(0)
    ctx_t = (et * alpha[:, :, None]).sum(0)
    return ctx_t


def param_init_feed_forward(O, params, prefix='ff', nin=None, nout=None,
                            orthogonal=True):
    """feedforward layer: affine transformation + point-wise nonlinearity"""

    if nin is None:
        nin = O['dim_proj']
    if nout is None:
        nout = O['dim_proj']
    params[_p(prefix, 'W')] = normal_weight(nin, nout, scale=0.01, orthogonal=orthogonal)
    params[_p(prefix, 'b')] = np.zeros((nout,), dtype=fX)

    return params


def feed_forward(P, state_below, O, prefix='rconv',
                 activ=tanh, **kwargs):
    if isinstance(activ, (str, unicode)):
        activ = eval(activ)
    return activ(T.dot(state_below, P[_p(prefix, 'W')]) + P[_p(prefix, 'b')])


def param_init_gru(O, params, prefix='gru', nin=None, dim=None, **kwargs):
    if nin is None:
        nin = O['dim_proj']
    if dim is None:
        dim = O['dim_proj']

    layer_id = kwargs.pop('layer_id', 0)
    context_dim = kwargs.pop('context_dim', None)

    # embedding to gates transformation weights, biases
    params[_p(prefix, 'W', layer_id)] = np.concatenate([normal_weight(nin, dim), normal_weight(nin, dim)], axis=1)
    params[_p(prefix, 'b', layer_id)] = np.zeros((2 * dim,), dtype=fX)

    # recurrent transformation weights for gates
    params[_p(prefix, 'U', layer_id)] = np.concatenate([orthogonal_weight(dim),
                                                        orthogonal_weight(dim)], axis=1)

    # embedding to hidden state proposal weights, biases
    params[_p(prefix, 'Wx', layer_id)] = normal_weight(nin, dim)
    params[_p(prefix, 'bx', layer_id)] = np.zeros((dim,), dtype=fX)

    # recurrent transformation weights for hidden state proposal
    params[_p(prefix, 'Ux', layer_id)] = orthogonal_weight(dim)

    if context_dim is not None:
        params[_p(prefix, 'Wc', layer_id)] = np.concatenate([normal_weight(context_dim, dim),
                                                             normal_weight(context_dim, dim)], axis=1)
        params[_p(prefix, 'Wcx', layer_id)] = normal_weight(context_dim, dim)

    return params


def _gru_step_slice(
        mask, x_, xx_,
        ht_1,
        U, Ux):
    """GRU step function to be used by scan

    arguments (0) | sequences (3) | outputs-info (1) | non-seqs (2)

    ht_1: ([BS], [H])
    U: ([H], [H] + [H])
    """

    _dim = Ux.shape[1]

    preact = T.dot(ht_1, U) + x_

    # reset and update gates
    r = T.nnet.sigmoid(_slice(preact, 0, _dim))
    u = T.nnet.sigmoid(_slice(preact, 1, _dim))

    # hidden state proposal
    ht_tilde = T.tanh(T.dot(ht_1, Ux) * r + xx_)

    # leaky integrate and obtain next hidden state
    ht = u * ht_1 + (1. - u) * ht_tilde
    ht = mask[:, None] * ht + (1. - mask)[:, None] * ht_1

    return ht


def _gru_step_slice_attention(
        mask, x_, xx_, context,
        ht_1,
        U, Ux, Wc, Wcx):
    """GRU step function with attention.
    
    context: ([BS], [Hc])
    Wc: ([Hc], [H] + [H])
    Wcx: ([Hc], [H])
    """

    _dim = Ux.shape[1]

    preact = T.nnet.sigmoid(T.dot(ht_1, U) + x_ + T.dot(context, Wc))

    # reset and update gates
    r = _slice(preact, 0, _dim)
    u = _slice(preact, 1, _dim)

    # hidden state proposal
    ht_tilde = T.tanh(T.dot(ht_1, Ux) * r + xx_ + T.dot(context, Wcx))

    # leaky integrate and obtain next hidden state
    ht = u * ht_1 + (1. - u) * ht_tilde
    ht = mask[:, None] * ht + (1. - mask)[:, None] * ht_1

    return ht


def gru_layer(P, state_below, O, prefix='gru', mask=None, **kwargs):
    """GRU layer
    
    input:
        state_below: ([Ts/t], [BS], x)     # x = [W] for src_embedding
        mask: ([Ts/t], [BS])
        context: ([Tt], [BS], [Hc])
    output: a list
        output[0]: hidden, ([Ts/t], [BS], [H])
    """

    layer_id = kwargs.pop('layer_id', 0)
    dropout_params = kwargs.pop('dropout_params', None)
    context = kwargs.pop('context', None)
    one_step = kwargs.pop('one_step', False)
    init_state = kwargs.pop('init_state', None)

    kw_ret = {}

    n_steps = state_below.shape[0]
    n_samples = state_below.shape[1] if state_below.ndim == 3 else 1
    dim = P[_p(prefix, 'Ux', layer_id)].shape[1]

    mask = T.alloc(1., n_steps, 1) if mask is None else mask

    # state_below is the input word embeddings
    # input to the gates, concatenated
    state_below_ = T.dot(state_below, P[_p(prefix, 'W', layer_id)]) + P[_p(prefix, 'b', layer_id)]
    # input to compute the hidden state proposal
    state_belowx = T.dot(state_below, P[_p(prefix, 'Wx', layer_id)]) + P[_p(prefix, 'bx', layer_id)]

    # prepare scan arguments
    init_states = [T.alloc(0., n_samples, dim) if init_state is None else init_state]

    if context is None:
        seqs = [mask, state_below_, state_belowx]
        shared_vars = [
            P[_p(prefix, 'U', layer_id)],
            P[_p(prefix, 'Ux', layer_id)],
        ]
        _step = _gru_step_slice
    else:
        seqs = [mask, state_below_, state_belowx, context]
        shared_vars = [
            P[_p(prefix, 'U', layer_id)],
            P[_p(prefix, 'Ux', layer_id)],
            P[_p(prefix, 'Wc', layer_id)],
            P[_p(prefix, 'Wcx', layer_id)],
        ]
        _step = _gru_step_slice_attention

    if one_step:
        outputs = _step(*(seqs + init_states + shared_vars))
    else:
        outputs, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=init_states,
            non_sequences=shared_vars,
            name=_p(prefix, '_layers', layer_id),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = outputs

    if dropout_params:
        outputs = dropout_layer(outputs, *dropout_params)

    return outputs, kw_ret


def param_init_multi_gru(O, params, prefix='gru', nin=None, dim=None, **kwargs):
    if nin is None:
        nin = O['dim_proj']
    if dim is None:
        dim = O['dim_proj']

    layer_id = kwargs.pop('layer_id', 0)
    context_dim = kwargs.pop('context_dim', None)
    unit_size = kwargs.pop('unit_size', 2)

    for i in xrange(1, unit_size + 1):
        # embedding to gates transformation weights, biases
        params[_p(prefix, 'W', layer_id, i)] = np.concatenate([normal_weight(nin, dim), normal_weight(nin, dim)],
                                                              axis=1)
        params[_p(prefix, 'b', layer_id, i)] = np.zeros((2 * dim,), dtype=fX)

        # recurrent transformation weights for gates
        params[_p(prefix, 'U', layer_id, i)] = np.concatenate([orthogonal_weight(dim),
                                                               orthogonal_weight(dim)], axis=1)

        # embedding to hidden state proposal weights, biases
        params[_p(prefix, 'Wx', layer_id, i)] = normal_weight(nin, dim)
        params[_p(prefix, 'bx', layer_id, i)] = np.zeros((dim,), dtype=fX)

        # recurrent transformation weights for hidden state proposal
        params[_p(prefix, 'Ux', layer_id, i)] = orthogonal_weight(dim)

        if context_dim is not None:
            params[_p(prefix, 'Wc', layer_id, i)] = np.concatenate([normal_weight(context_dim, dim),
                                                                    normal_weight(context_dim, dim)], axis=1)
            params[_p(prefix, 'Wcx', layer_id, i)] = normal_weight(context_dim, dim)

    return params


def _multi_gru_step_slice_2(
        mask, x_0, x_1, xx_0, xx_1,
        ht_1,
        U_0, U_1, Ux_0, Ux_1,
):
    _dim = Ux_0.shape[1]

    h = ht_1

    x_list = x_0, x_1
    xx_list = xx_0, xx_1
    U_list = U_0, U_1
    Ux_list = Ux_0, Ux_1

    for i in range(2):
        x_, xx_, U, Ux = x_list[i], xx_list[i], U_list[i], Ux_list[i]

        preact = T.dot(h, U) + x_

        # reset and update gates
        r = T.nnet.sigmoid(_slice(preact, 0, _dim))
        u = T.nnet.sigmoid(_slice(preact, 1, _dim))

        # hidden state proposal
        ht_tilde = T.tanh(T.dot(h, Ux) * r + xx_)

        # leaky integrate and obtain next hidden state
        ht = u * h + (1. - u) * ht_tilde
        h = mask[:, None] * ht + (1. - mask)[:, None] * h

    return h


def _multi_gru_step_slice_attention_2(
        mask, x_0, x_1, xx_0, xx_1, context,
        ht_1,
        U_0, U_1, Ux_0, Ux_1, Wc_0, Wc_1, Wcx_0, Wcx_1,
):
    _dim = Ux_0.shape[1]

    h = ht_1

    x_list = x_0, x_1
    xx_list = xx_0, xx_1
    U_list = U_0, U_1
    Ux_list = Ux_0, Ux_1
    Wc_list = Wc_0, Wc_1
    Wcx_list = Wcx_0, Wcx_1

    for i in range(2):
        x_, xx_, U, Ux, Wc, Wcx = x_list[i], xx_list[i], U_list[i], Ux_list[i], Wc_list[i], Wcx_list[i]

        preact = T.nnet.sigmoid(T.dot(h, U) + x_ + T.dot(context, Wc))

        # reset and update gates
        r = _slice(preact, 0, _dim)
        u = _slice(preact, 1, _dim)

        # hidden state proposal
        ht_tilde = T.tanh(T.dot(h, Ux) * r + xx_ + T.dot(context, Wcx))

        # leaky integrate and obtain next hidden state
        ht = u * h + (1. - u) * ht_tilde
        h = mask[:, None] * ht + (1. - mask)[:, None] * h

    return h


def multi_gru_layer(P, state_below, O, prefix='multi_gru', mask=None, **kwargs):
    """Multi-GRU layer, deep in time.
    
    x, h_t -> h'
    x, h' -> h_{t+1}
    """

    layer_id = kwargs.pop('layer_id', 0)
    dropout_params = kwargs.pop('dropout_params', None)
    context = kwargs.pop('context', None)
    one_step = kwargs.pop('one_step', False)
    init_state = kwargs.pop('init_state', None)
    unit_size = kwargs.pop('unit_size', 2)

    kw_ret = {}

    n_steps = state_below.shape[0]
    n_samples = state_below.shape[1] if state_below.ndim == 3 else 1
    dim = P[_p(prefix, 'Ux', layer_id, 1)].shape[1]

    mask = T.alloc(1., n_steps, 1) if mask is None else mask

    # state_below is the input word embeddings
    # List of inputs and hidden state proposals
    # FIXME: index of unit start at 1, NOT 0!
    state_belows = [
        T.dot(state_below, P[_p(prefix, 'W', layer_id, i)]) + P[_p(prefix, 'b', layer_id, i)]
        for i in xrange(1, unit_size + 1)
        ]
    state_belowxs = [
        T.dot(state_below, P[_p(prefix, 'Wx', layer_id, i)]) + P[_p(prefix, 'bx', layer_id, i)]
        for i in xrange(1, unit_size + 1)
        ]

    # prepare scan arguments
    init_states = [T.alloc(0., n_samples, dim) if init_state is None else init_state]

    if context is None:
        seqs = [mask] + state_belows + state_belowxs
        shared_vars = [P[_p(prefix, 'U', layer_id, i)] for i in xrange(1, unit_size + 1)] + \
                      [P[_p(prefix, 'Ux', layer_id, i)] for i in xrange(1, unit_size + 1)]
        _step = _multi_gru_step_slice_2
    else:
        seqs = [mask] + state_belows + state_belowxs + [context]
        shared_vars = [P[_p(prefix, 'U', layer_id, i)] for i in xrange(1, unit_size + 1)] + \
                      [P[_p(prefix, 'Ux', layer_id, i)] for i in xrange(1, unit_size + 1)] + \
                      [P[_p(prefix, 'Wc', layer_id, i)] for i in xrange(1, unit_size + 1)] + \
                      [P[_p(prefix, 'Wcx', layer_id, i)] for i in xrange(1, unit_size + 1)]
        _step = _multi_gru_step_slice_attention_2

    if one_step:
        outputs = _step(*(seqs + init_states + shared_vars))
    else:
        outputs, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=init_states,
            non_sequences=shared_vars,
            name=_p(prefix, '_layers', layer_id),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = outputs

    if dropout_params:
        outputs = dropout_layer(outputs, *dropout_params)

    return outputs, kw_ret


def param_init_gru_cond(O, params, prefix='gru_cond', nin=None, dim=None, dimctx=None, nin_nonlin=None,
                        dim_nonlin=None, **kwargs):
    if nin is None:
        nin = O['dim']
    if dim is None:
        dim = O['dim']
    if dimctx is None:
        dimctx = O['dim']
    if nin_nonlin is None:
        nin_nonlin = nin
    if dim_nonlin is None:
        dim_nonlin = dim
    layer_id = kwargs.pop('layer_id', 0)

    params[_p(prefix, 'W', layer_id)] = np.concatenate([normal_weight(nin, dim),
                                                        normal_weight(nin, dim)], axis=1)
    params[_p(prefix, 'b', layer_id)] = np.zeros((2 * dim,), dtype=fX)
    params[_p(prefix, 'U', layer_id)] = np.concatenate([orthogonal_weight(dim_nonlin),
                                                        orthogonal_weight(dim_nonlin)], axis=1)

    params[_p(prefix, 'Wx', layer_id)] = normal_weight(nin_nonlin, dim_nonlin)
    params[_p(prefix, 'Ux', layer_id)] = orthogonal_weight(dim_nonlin)
    params[_p(prefix, 'bx', layer_id)] = np.zeros((dim_nonlin,), dtype=fX)

    params[_p(prefix, 'U_nl', layer_id)] = np.concatenate([orthogonal_weight(dim_nonlin),
                                                           orthogonal_weight(dim_nonlin)], axis=1)
    params[_p(prefix, 'b_nl', layer_id)] = np.zeros((2 * dim_nonlin,), dtype=fX)

    params[_p(prefix, 'Ux_nl', layer_id)] = orthogonal_weight(dim_nonlin)
    params[_p(prefix, 'bx_nl', layer_id)] = np.zeros((dim_nonlin,), dtype=fX)

    # context to LSTM
    params[_p(prefix, 'Wc', layer_id)] = normal_weight(dimctx, dim * 2)

    params[_p(prefix, 'Wcx', layer_id)] = normal_weight(dimctx, dim)

    # attention: combined -> hidden
    params[_p(prefix, 'W_comb_att', layer_id)] = normal_weight(dim, dimctx)

    # attention: context -> hidden
    params[_p(prefix, 'Wc_att', layer_id)] = normal_weight(dimctx)

    # attention: hidden bias
    params[_p(prefix, 'b_att', layer_id)] = np.zeros((dimctx,), dtype=fX)

    # attention:
    params[_p(prefix, 'U_att', layer_id)] = normal_weight(dimctx, 1)
    params[_p(prefix, 'c_tt', layer_id)] = np.zeros((1,), dtype=fX)

    return params


def gru_cond_layer(P, state_below, O, prefix='gru', mask=None, context=None, one_step=False, init_memory=None,
                   init_state=None, context_mask=None, **kwargs):
    """Conditional GRU layer with Attention
    
    input:
        state_below: ([Tt], [BS], x)    # x = [W] for tgt_embedding
        mask: ([Tt], [BS])
        init_state: ([BS], [H])
        context: ([Tt], [BS], [Hc])
        context_mask: ([Tt], [BS])
    
    :return list of 3 outputs
        hidden_decoder: ([Tt], [BS], [H]), hidden states of the decoder gru
        context_decoder: ([Tt], [BS], [Hc]), weighted averages of context, generated by attention module
        alpha_decoder: ([Tt], [Bs], [Tt]), weights (alignment matrix)
    """

    layer_id = kwargs.pop('layer_id', 0)

    kw_ret = {}

    assert context, 'Context must be provided'
    assert context.ndim == 3, 'Context must be 3-d: #annotation * #sample * dim'
    if one_step:
        assert init_state, 'previous state must be provided'

    # Dimensions
    n_steps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1
    dim = P[_p(prefix, 'Wcx', layer_id)].shape[1]
    dropout_params = kwargs.pop('dropout_params', None)

    # Mask
    if mask is None:
        mask = T.alloc(1., n_steps, 1)

    # Initial/previous state
    if init_state is None:
        init_state = T.alloc(0., n_samples, dim)

    projected_context = T.dot(context, P[_p(prefix, 'Wc_att', layer_id)]) + P[_p(prefix, 'b_att', layer_id)]

    # projected x
    state_belowx = T.dot(state_below, P[_p(prefix, 'Wx', layer_id)]) + P[_p(prefix, 'bx', layer_id)]
    state_below_ = T.dot(state_below, P[_p(prefix, 'W', layer_id)]) + P[_p(prefix, 'b', layer_id)]

    def _step_slice(m_, x_, xx_,
                    h_, ctx_, alpha_,
                    projected_context_, context_,
                    U, Wc, W_comb_att, U_att, c_tt, Ux, Wcx, U_nl, Ux_nl, b_nl, bx_nl):
        h1 = _gru_step_slice(m_, x_, xx_, h_, U, Ux)

        # attention
        pstate_ = T.dot(h1, W_comb_att)
        pctx__ = projected_context_ + pstate_[None, :, :]
        # pctx__ += xc_
        pctx__ = T.tanh(pctx__)

        alpha = T.dot(pctx__, U_att) + c_tt
        alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
        alpha = T.exp(alpha)
        if context_mask:
            alpha = alpha * context_mask
        alpha = alpha / alpha.sum(0, keepdims=True)
        ctx_ = (context_ * alpha[:, :, None]).sum(0)  # current context

        # GRU 2 (with attention)
        preact2 = T.nnet.sigmoid(T.dot(h1, U_nl) + b_nl + T.dot(ctx_, Wc))

        r2 = _slice(preact2, 0, dim)
        u2 = _slice(preact2, 1, dim)

        preactx2 = (T.dot(h1, Ux_nl) + bx_nl) * r2 + T.dot(ctx_, Wcx)

        h2 = T.tanh(preactx2)

        h2 = u2 * h1 + (1. - u2) * h2
        h2 = m_[:, None] * h2 + (1. - m_)[:, None] * h1

        return h2, ctx_, alpha.T

    seqs = [mask, state_below_, state_belowx]
    _step = _step_slice

    shared_vars = [P[_p(prefix, 'U', layer_id)],
                   P[_p(prefix, 'Wc', layer_id)],
                   P[_p(prefix, 'W_comb_att', layer_id)],
                   P[_p(prefix, 'U_att', layer_id)],
                   P[_p(prefix, 'c_tt', layer_id)],
                   P[_p(prefix, 'Ux', layer_id)],
                   P[_p(prefix, 'Wcx', layer_id)],
                   P[_p(prefix, 'U_nl', layer_id)],
                   P[_p(prefix, 'Ux_nl', layer_id)],
                   P[_p(prefix, 'b_nl', layer_id)],
                   P[_p(prefix, 'bx_nl', layer_id)]]

    if one_step:
        result = _step(*(seqs + [init_state, None, None, projected_context, context] + shared_vars))
    else:
        result, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=[init_state,
                          T.alloc(0., n_samples, context.shape[2]),
                          T.alloc(0., n_samples, context.shape[0])],
            non_sequences=[projected_context, context] + shared_vars,
            name=_p(prefix, '_layers'),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = result[0]

    result = list(result)
    result.append(kw_ret)

    if dropout_params:
        result[0] = dropout_layer(result[0], *dropout_params)

    return result


def param_init_multi_gru_cond(O, params, prefix='gru_cond', nin=None, dim=None, dimctx=None, nin_nonlin=None,
                              dim_nonlin=None, **kwargs):
    if nin is None:
        nin = O['dim']
    if dim is None:
        dim = O['dim']
    if dimctx is None:
        dimctx = O['dim']
    if nin_nonlin is None:
        nin_nonlin = nin
    if dim_nonlin is None:
        dim_nonlin = dim
    layer_id = kwargs.pop('layer_id', 0)
    cond_unit_size = kwargs.pop('cond_unit_size', 2)

    for i in xrange(1, cond_unit_size + 1):
        params[_p(prefix, 'W', layer_id, i)] = np.concatenate([normal_weight(nin, dim),
                                                               normal_weight(nin, dim)], axis=1)
        params[_p(prefix, 'b', layer_id, i)] = np.zeros((2 * dim,), dtype=fX)
        params[_p(prefix, 'U', layer_id, i)] = np.concatenate([orthogonal_weight(dim_nonlin),
                                                               orthogonal_weight(dim_nonlin)], axis=1)

        params[_p(prefix, 'Wx', layer_id, i)] = normal_weight(nin_nonlin, dim_nonlin)
        params[_p(prefix, 'Ux', layer_id, i)] = orthogonal_weight(dim_nonlin)
        params[_p(prefix, 'bx', layer_id, i)] = np.zeros((dim_nonlin,), dtype=fX)

        params[_p(prefix, 'U_nl', layer_id, i)] = np.concatenate([orthogonal_weight(dim_nonlin),
                                                                  orthogonal_weight(dim_nonlin)], axis=1)
        params[_p(prefix, 'b_nl', layer_id, i)] = np.zeros((2 * dim_nonlin,), dtype=fX)

        params[_p(prefix, 'Ux_nl', layer_id, i)] = orthogonal_weight(dim_nonlin)
        params[_p(prefix, 'bx_nl', layer_id, i)] = np.zeros((dim_nonlin,), dtype=fX)

        # context to LSTM
        params[_p(prefix, 'Wc', layer_id, i)] = normal_weight(dimctx, dim * 2)

        params[_p(prefix, 'Wcx', layer_id, i)] = normal_weight(dimctx, dim)

    # attention: combined -> hidden
    params[_p(prefix, 'W_comb_att', layer_id)] = normal_weight(dim, dimctx)

    # attention: context -> hidden
    params[_p(prefix, 'Wc_att', layer_id)] = normal_weight(dimctx)

    # attention: hidden bias
    params[_p(prefix, 'b_att', layer_id)] = np.zeros((dimctx,), dtype=fX)

    # attention:
    params[_p(prefix, 'U_att', layer_id)] = normal_weight(dimctx, 1)
    params[_p(prefix, 'c_tt', layer_id)] = np.zeros((1,), dtype=fX)

    return params


def multi_gru_cond_layer(P, state_below, O, prefix='gru', mask=None, context=None, one_step=False, init_memory=None,
                         init_state=None, context_mask=None, **kwargs):
    layer_id = kwargs.pop('layer_id', 0)
    cond_unit_size = kwargs.pop('cond_unit_size', 2)

    theano.config.exception_verbosity = 'high'

    kw_ret = {}

    assert context, 'Context must be provided'
    assert context.ndim == 3, 'Context must be 3-d: #annotation * #sample * dim'
    if one_step:
        assert init_state, 'previous state must be provided'

    # Dimensions
    n_steps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1
    dim = P[_p(prefix, 'Wcx', layer_id, 1)].shape[1]
    dropout_params = kwargs.pop('dropout_params', None)

    # Mask
    if mask is None:
        mask = T.alloc(1., n_steps, 1)

    # Initial/previous state
    if init_state is None:
        init_state = T.alloc(0., n_samples, dim)

    projected_context = T.dot(context, P[_p(prefix, 'Wc_att', layer_id)]) + P[_p(prefix, 'b_att', layer_id)]

    # projected x
    state_belows = [
        T.dot(state_below, P[_p(prefix, 'W', layer_id, i)]) + P[_p(prefix, 'b', layer_id, i)]
        for i in xrange(1, cond_unit_size + 1)
    ]
    state_belowxs = [
        T.dot(state_below, P[_p(prefix, 'Wx', layer_id, i)]) + P[_p(prefix, 'bx', layer_id, i)]
        for i in xrange(1, cond_unit_size + 1)
    ]

    def _step_slice(m_, x_0, x_1, xx_0, xx_1,
                    h_, ctx_, alpha_,
                    projected_context_, context_,
                    U_0, U_1, Wc_0, Wc_1, W_comb_att, U_att, c_tt, Ux_0, Ux_1, Wcx_0, Wcx_1,
                    U_nl_0, U_nl_1, Ux_nl_0, Ux_nl_1, b_nl_0, b_nl_1, bx_nl_0, bx_nl_1):
        h1 = _multi_gru_step_slice_2(m_, x_0, x_1, xx_0, xx_1, h_, U_0, U_1, Ux_0, Ux_1)

        # attention
        pstate_ = T.dot(h1, W_comb_att)
        pctx__ = projected_context_ + pstate_[None, :, :]
        # pctx__ += xc_
        pctx__ = T.tanh(pctx__)

        alpha = T.dot(pctx__, U_att) + c_tt
        alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
        alpha = T.exp(alpha)
        if context_mask:
            alpha = alpha * context_mask
        alpha = alpha / alpha.sum(0, keepdims=True)
        ctx_ = (context_ * alpha[:, :, None]).sum(0)  # current context

        # GRU 2 (with attention)
        U_nl_list = U_nl_0, U_nl_1
        b_nl_list = b_nl_0, b_nl_1
        Wc_list = Wc_0, Wc_1
        Ux_nl_list = Ux_nl_0, Ux_nl_1
        bx_nl_list = bx_nl_0, bx_nl_1
        Wcx_list = Wcx_0, Wcx_1

        h = h1
        for i in range(2):
            U_nl, b_nl, Wc, Ux_nl, bx_nl, Wcx = \
                U_nl_list[i], b_nl_list[i], Wc_list[i], Ux_nl_list[i], bx_nl_list[i], Wcx_list[i]

            preact2 = T.nnet.sigmoid(T.dot(h, U_nl) + b_nl + T.dot(ctx_, Wc))

            r2 = _slice(preact2, 0, dim)
            u2 = _slice(preact2, 1, dim)

            preactx2 = (T.dot(h1, Ux_nl) + bx_nl) * r2 + T.dot(ctx_, Wcx)

            h2 = T.tanh(preactx2)

            h2 = u2 * h + (1. - u2) * h2
            h = m_[:, None] * h2 + (1. - m_)[:, None] * h

        return h, ctx_, alpha.T

    seqs = [mask] + state_belows + state_belowxs
    _step = _step_slice

    shared_vars = [P[_p(prefix, 'U', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'Wc', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'W_comb_att', layer_id)]] + \
                  [P[_p(prefix, 'U_att', layer_id)]] + \
                  [P[_p(prefix, 'c_tt', layer_id)]] + \
                  [P[_p(prefix, 'Ux', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'Wcx', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'U_nl', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'Ux_nl', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'b_nl', layer_id, i)] for i in xrange(1, cond_unit_size + 1)] + \
                  [P[_p(prefix, 'bx_nl', layer_id, i)] for i in xrange(1, cond_unit_size + 1)]

    if one_step:
        result = _step(*(seqs + [init_state, None, None, projected_context, context] + shared_vars))
    else:
        result, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=[init_state,
                          T.alloc(0., n_samples, context.shape[2]),
                          T.alloc(0., n_samples, context.shape[0])],
            non_sequences=[projected_context, context] + shared_vars,
            name=_p(prefix, '_layers'),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = result[0]

    result = list(result)
    result.append(kw_ret)

    if dropout_params:
        result[0] = dropout_layer(result[0], *dropout_params)

    return result


def param_init_lstm(O, params, prefix='lstm', nin=None, dim=None, **kwargs):
    if nin is None:
        nin = O['dim_proj']
    if dim is None:
        dim = O['dim_proj']

    layer_id = kwargs.pop('layer_id', 0)
    context_dim = kwargs.pop('context_dim', None)

    params[_p(prefix, 'W', layer_id)] = np.concatenate([
        normal_weight(nin, dim),
        normal_weight(nin, dim),
        normal_weight(nin, dim),
        normal_weight(nin, dim),
    ], axis=1)

    params[_p(prefix, 'U', layer_id)] = np.concatenate([
        orthogonal_weight(dim),
        orthogonal_weight(dim),
        orthogonal_weight(dim),
        orthogonal_weight(dim),
    ], axis=1)

    params[_p(prefix, 'b', layer_id)] = np.zeros((4 * dim,), dtype=fX)

    if context_dim is not None:
        params[_p(prefix, 'Wc', layer_id)] = np.concatenate([
            normal_weight(context_dim, dim),
            normal_weight(context_dim, dim),
            normal_weight(context_dim, dim),
            normal_weight(context_dim, dim),
        ], axis=1)

    return params


def _lstm_step_kernel(preact, mask_, h_, c_, _dim):
    i = T.nnet.sigmoid(_slice(preact, 0, _dim))
    f = T.nnet.sigmoid(_slice(preact, 1, _dim))
    o = T.nnet.sigmoid(_slice(preact, 2, _dim))
    c = T.tanh(_slice(preact, 3, _dim))

    c = f * c_ + i * c
    c = mask_[:, None] * c + (1. - mask_)[:, None] * c_

    h = o * T.tanh(c)
    h = mask_[:, None] * h + (1. - mask_)[:, None] * h_

    return i, f, o, c, h


def _lstm_step_slice(
        mask_, x_,
        h_, c_,
        U):
    _dim = U.shape[1] // 4
    preact = T.dot(h_, U) + x_

    i, f, o, c, h = _lstm_step_kernel(preact, mask_, h_, c_, _dim)
    return h, c


def _lstm_step_slice_gates(
        mask_, x_,
        h_, c_, i_, f_, o_,
        U):
    _dim = U.shape[1] // 4
    preact = T.dot(h_, U) + x_

    i, f, o, c, h = _lstm_step_kernel(preact, mask_, h_, c_, _dim)
    return h, c, i, f, o


def _lstm_step_slice_attention(
        mask_, x_, context,
        h_, c_,
        U, Wc):
    _dim = U.shape[1] // 4
    preact = T.dot(h_, U) + x_ + T.dot(context, Wc)

    i, f, o, c, h = _lstm_step_kernel(preact, mask_, h_, c_, _dim)
    return h, c


def _lstm_step_slice_attention_gates(
        mask_, x_, context,
        h_, c_, i_, f_, o_,
        U, Wc):
    _dim = U.shape[1] // 4
    preact = T.dot(h_, U) + x_ + T.dot(context, Wc)

    i, f, o, c, h = _lstm_step_kernel(preact, mask_, h_, c_, _dim)
    return h, c, i, f, o


def lstm_layer(P, state_below, O, prefix='lstm', mask=None, **kwargs):
    """LSTM layer
    
    inputs and outputs are same as GRU layer.
    
    outputs[1]: hidden memory
    """

    layer_id = kwargs.pop('layer_id', 0)
    dropout_params = kwargs.pop('dropout_params', None)
    context = kwargs.pop('context', None)
    one_step = kwargs.pop('one_step', False)
    init_state = kwargs.pop('init_state', None)
    init_memory = kwargs.pop('init_memory', None)
    get_gates = kwargs.pop('get_gates', False)

    kw_ret = {}

    n_steps = state_below.shape[0]
    n_samples = state_below.shape[1] if state_below.ndim == 3 else 1
    dim = P[_p(prefix, 'U', layer_id)].shape[1] // 4

    mask = T.alloc(1., n_steps, 1) if mask is None else mask

    state_below = T.dot(state_below, P[_p(prefix, 'W', layer_id)]) + P[_p(prefix, 'b', layer_id)]

    # prepare scan arguments
    init_states = [T.alloc(0., n_samples, dim) if init_state is None else init_state,
                   T.alloc(0., n_samples, dim) if init_memory is None else init_memory, ]

    if get_gates:
        init_states.extend([T.alloc(0., n_samples, dim) for _ in range(3)])

    if context is None:
        seqs = [mask, state_below]
        shared_vars = [P[_p(prefix, 'U', layer_id)]]
    else:
        seqs = [mask, state_below, context]
        shared_vars = [P[_p(prefix, 'U', layer_id)],
                       P[_p(prefix, 'Wc', layer_id)]]

    step_str = '_lstm_step'
    if context is not None:
        step_str += '_attention'
    if get_gates:
        step_str += '_gates'
    _step = eval(step_str)

    if one_step:
        outputs = _step(*(seqs + init_states + shared_vars))
    else:
        outputs, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=init_states,
            non_sequences=shared_vars,
            name=_p(prefix, '_layers', layer_id),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = outputs[0]
    kw_ret['memory_output'] = outputs[1]

    if get_gates:
        kw_ret['input_gates'] = outputs[2]
        kw_ret['forget_gates'] = outputs[3]
        kw_ret['output_gates'] = outputs[4]

    outputs = [outputs[0], outputs[1], kw_ret]

    if dropout_params:
        outputs[0] = dropout_layer(outputs[0], *dropout_params)

    return outputs


def param_init_lstm_cond(O, params, prefix='lstm_cond', nin=None, dim=None, dimctx=None, nin_nonlin=None,
                         dim_nonlin=None, **kwargs):
    if nin is None:
        nin = O['dim']
    if dim is None:
        dim = O['dim']
    if dimctx is None:
        dimctx = O['dim']
    if nin_nonlin is None:
        nin_nonlin = nin
    if dim_nonlin is None:
        dim_nonlin = dim
    layer_id = kwargs.pop('layer_id', 0)

    W = np.concatenate([
        normal_weight(nin, dim),
        normal_weight(nin, dim),
        normal_weight(nin, dim),
        normal_weight(nin_nonlin, dim),
    ], axis=1)
    params[_p(prefix, 'W', layer_id)] = W
    params[_p(prefix, 'b', layer_id)] = np.zeros((4 * dim,), dtype=fX)
    U = np.concatenate([
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
    ], axis=1)
    params[_p(prefix, 'U', layer_id)] = U

    U_nl = np.concatenate([
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
        orthogonal_weight(dim_nonlin),
    ], axis=1)
    params[_p(prefix, 'U_nl', layer_id)] = U_nl
    params[_p(prefix, 'b_nl', layer_id)] = np.zeros((4 * dim_nonlin,), dtype=fX)

    # context to LSTM
    Wc = normal_weight(dimctx, dim * 4)
    params[_p(prefix, 'Wc', layer_id)] = Wc

    # attention: combined -> hidden
    W_comb_att = normal_weight(dim, dimctx)
    params[_p(prefix, 'W_comb_att', layer_id)] = W_comb_att

    # attention: context -> hidden
    Wc_att = normal_weight(dimctx)
    params[_p(prefix, 'Wc_att', layer_id)] = Wc_att

    # attention: hidden bias
    b_att = np.zeros((dimctx,), dtype=fX)
    params[_p(prefix, 'b_att', layer_id)] = b_att

    # attention:
    U_att = normal_weight(dimctx, 1)
    params[_p(prefix, 'U_att', layer_id)] = U_att
    c_att = np.zeros((1,), dtype=fX)
    params[_p(prefix, 'c_tt', layer_id)] = c_att

    return params


def _lstm_step_cond_kernel(
        x_, dim, mask_, c_, h_, U,
        W_comb_att, projected_context_, U_att, c_tt, context_mask, context_,
        U_nl, b_nl, Wc,
):
    # LSTM 1
    preact1 = T.dot(h_, U) + x_

    i1 = T.nnet.sigmoid(_slice(preact1, 0, dim))
    f1 = T.nnet.sigmoid(_slice(preact1, 1, dim))
    o1 = T.nnet.sigmoid(_slice(preact1, 2, dim))
    c1 = T.tanh(_slice(preact1, 3, dim))

    c1 = f1 * c_ + i1 * c1
    c1 = mask_[:, None] * c1 + (1. - mask_)[:, None] * c_

    h1 = o1 * T.tanh(c1)
    h1 = mask_[:, None] * h1 + (1. - mask_)[:, None] * h_

    # Attention
    pstate_ = T.dot(h1, W_comb_att)
    pctx__ = projected_context_ + pstate_[None, :, :]
    # pctx__ += xc_
    pctx__ = T.tanh(pctx__)

    alpha = T.dot(pctx__, U_att) + c_tt
    alpha = alpha.reshape([alpha.shape[0], alpha.shape[1]])
    alpha = T.exp(alpha)
    if context_mask:
        alpha = alpha * context_mask
    alpha = alpha / alpha.sum(0, keepdims=True)
    ctx_ = (context_ * alpha[:, :, None]).sum(0)  # current context

    # LSTM 2 (with attention)
    preact2 = T.dot(h1, U_nl) + b_nl + T.dot(ctx_, Wc)

    i2 = T.nnet.sigmoid(_slice(preact2, 0, dim))
    f2 = T.nnet.sigmoid(_slice(preact2, 1, dim))
    o2 = T.nnet.sigmoid(_slice(preact2, 2, dim))
    c2 = T.tanh(_slice(preact2, 3, dim))

    c2 = f2 * c1 + i2 * c2
    c2 = mask_[:, None] * c2 + (1. - mask_)[:, None] * c1

    h2 = o2 * T.tanh(c2)
    h2 = mask_[:, None] * h2 + (1. - mask_)[:, None] * h1

    return h2, c2, ctx_, alpha, i1, f1, o1, i2, f2, o2


def lstm_cond_layer(P, state_below, O, prefix='lstm', mask=None, context=None, one_step=False, init_memory=None,
                    init_state=None, context_mask=None, **kwargs):
    """Conditional LSTM layer with attention
    
    inputs and outputs are same as GRU cond layer.
    """

    layer_id = kwargs.pop('layer_id', 0)
    get_gates = kwargs.pop('get_gates', False)

    kw_ret = {}

    assert context, 'Context must be provided'
    assert context.ndim == 3, 'Context must be 3-d: #annotation * #sample * dim'
    if one_step:
        assert init_state, 'previous state must be provided'

    # Dimensions
    n_steps = state_below.shape[0]
    if state_below.ndim == 3:
        n_samples = state_below.shape[1]
    else:
        n_samples = 1
    dim = P[_p(prefix, 'Wc', layer_id)].shape[1] // 4
    dropout_params = kwargs.pop('dropout_params', None)

    # Mask
    if mask is None:
        mask = T.alloc(1., n_steps, 1)

    # Initial/previous state
    if init_state is None:
        init_state = T.alloc(0., n_samples, dim)

    projected_context = T.dot(context, P[_p(prefix, 'Wc_att', layer_id)]) + P[_p(prefix, 'b_att', layer_id)]

    # Projected x
    state_below = T.dot(state_below, P[_p(prefix, 'W', layer_id)]) + P[_p(prefix, 'b', layer_id)]

    def _step_slice(mask_, x_,
                    h_, c_, ctx_, alpha_,
                    projected_context_, context_,
                    U, Wc, W_comb_att, U_att, c_tt, U_nl, b_nl):
        h2, c2, ctx_, alpha, i1, f1, o1, i2, f2, o2 = _lstm_step_cond_kernel(
            x_, dim, mask_, c_, h_, U, W_comb_att, projected_context_, U_att, c_tt, context_mask,
            context_, U_nl, b_nl, Wc,
        )

        return h2, c2, ctx_, alpha.T

    def _step_slice_gates(
            mask_, x_,
            h_, c_, ctx_, alpha_, i1_, f1_, o1_, i2_, f2_, o2_,
            projected_context_, context_,
            U, Wc, W_comb_att, U_att, c_tt, U_nl, b_nl):
        h2, c2, ctx_, alpha, i1, f1, o1, i2, f2, o2 = _lstm_step_cond_kernel(
            x_, dim, mask_, c_, h_, U, W_comb_att, projected_context_, U_att, c_tt, context_mask,
            context_, U_nl, b_nl, Wc,
        )

        return h2, c2, ctx_, alpha.T, i1, f1, o1, i2, f2, o2

    # Prepare scan arguments
    seqs = [mask, state_below]
    init_states = [
        init_state,
        T.alloc(0., n_samples, dim) if init_memory is None else init_memory,
        T.alloc(0., n_samples, context.shape[2]),
        T.alloc(0., n_samples, context.shape[0]),
    ]
    if get_gates:
        init_states.extend([T.alloc(0., n_samples, dim) for _ in range(6)])

    shared_vars = [
        P[_p(prefix, 'U', layer_id)],
        P[_p(prefix, 'Wc', layer_id)],
        P[_p(prefix, 'W_comb_att', layer_id)],
        P[_p(prefix, 'U_att', layer_id)],
        P[_p(prefix, 'c_tt', layer_id)],
        P[_p(prefix, 'U_nl', layer_id)],
        P[_p(prefix, 'b_nl', layer_id)],
    ]

    step_str = '_step_slice'
    if get_gates:
        step_str += '_gates'
    _step = eval(step_str)

    if one_step:
        result = _step(*(seqs + init_states + [projected_context, context] + shared_vars))
    else:
        result, _ = theano.scan(
            _step,
            sequences=seqs,
            outputs_info=init_states,
            non_sequences=[projected_context, context] + shared_vars,
            name=_p(prefix, '_layers'),
            n_steps=n_steps,
            profile=profile,
            strict=True,
        )

    kw_ret['hidden_without_dropout'] = result[0]
    kw_ret['memory_output'] = result[1]

    if get_gates:
        kw_ret['input_gates'] = result[4]
        kw_ret['forget_gates'] = result[5]
        kw_ret['output_gates'] = result[6]
        kw_ret['input_gates2'] = result[7]
        kw_ret['forget_gates2'] = result[8]
        kw_ret['output_gates2'] = result[9]

    result = list(result)

    if dropout_params:
        result[0] = dropout_layer(result[0], *dropout_params)

    # Return memory c at the last in kw_ret
    return result[0], result[2], result[3], kw_ret


# layers: 'name': ('parameter initializer', 'builder')
layers = {
    'ff': (param_init_feed_forward, feed_forward),
    'gru': (param_init_gru, gru_layer),
    'gru_cond': (param_init_gru_cond, gru_cond_layer),
    'multi_gru': (param_init_multi_gru, multi_gru_layer),
    'multi_gru_cond': (param_init_multi_gru_cond, multi_gru_cond_layer),
    'lstm': (param_init_lstm, lstm_layer),
    'lstm_cond': (param_init_lstm_cond, lstm_cond_layer),
}


def get_layer(name):
    fns = layers[name]
    return fns[0], fns[1]


def get_init(name):
    return layers[name][0]


def get_build(name):
    return layers[name][1]


__all__ = [
    'tanh',
    'linear',
    'attention_layer',
    'param_init_feed_forward',
    'feed_forward',
    'param_init_gru',
    'gru_layer',
    'param_init_gru_cond',
    'gru_cond_layer',
    'layers',
    'get_layer',
    'get_build',
    'get_init',
]
