from __future__ import absolute_import
import numpy as np
from .Node import Op
from ..gpu_links import quantize_embedding_with_scale, \
    quantized_embedding_lookup_with_scale, \
    lsq_rounding, lsq_rounding_gradient
from ..ndarray import empty
from .EmbeddingLookUp import embedding_lookup_gradient_with_lookup_op, embedding_lookup_gradient_dedupgrad_op
from .MultiplyElewise import mul_op


class ALPTEmbeddingLookUpOp(Op):
    def __init__(self, embed, indices, scale, zero_point, digit, ctx=None):
        assert digit in (8, 16)
        super().__init__(ALPTEmbeddingLookUpOp,
                         [embed, indices, scale], ctx)
        self.digit = digit
        self.middle = zero_point
        self.grad_node = None
        if self.digit == 8:
            dtype = np.int8
        else:
            dtype = np.int16
        embed.dtype = dtype
        embed.is_embed = True

    def compute(self, input_vals, output_val, stream_handle=None):
        if self.on_cpu:
            raise NotImplementedError
        else:
            quantized_embedding_lookup_with_scale(
                input_vals[0], input_vals[1], input_vals[2], output_val, self.digit, self.middle, stream_handle)

    def gradient(self, output_grad):
        self.grad_node = embedding_lookup_gradient_with_lookup_op(
            output_grad, self.inputs[1], self, None, ctx=self.raw_ctx)
        grad_node = embedding_lookup_gradient_dedupgrad_op(
            self.grad_node, output_grad, ctx=self.raw_ctx)
        return [grad_node, None, None]

    def infer_shape(self, input_shapes):
        assert len(input_shapes) == 3
        if self.grad_node is not None:
            self.grad_node.embed_shape = input_shapes[0]
        if self.scale_grad_node is not None:
            self.scale_grad_node.scale_shape = input_shapes[2]
        output_shape = list(input_shapes[1])
        output_shape.append(input_shapes[0][1])
        return tuple(output_shape)

    def forward_hook(self, config):
        super().forward_hook(config)
        embed_var = self.inputs[0]
        ori_embed = config.placeholder_to_arr_map[embed_var]
        scale_var = self.inputs[2]
        cur_scale = config.placeholder_to_arr_map[scale_var]
        dtype = embed_var.dtype
        new_embed = empty(ori_embed.shape, ctx=self.ctx,
                          dtype=dtype, force32=False)
        config.placeholder_to_arr_map[embed_var] = new_embed
        quantize_embedding_with_scale(
            ori_embed, cur_scale, new_embed,  self.middle, self.digit, config.comp_stream)
        config.comp_stream.sync()


def alpt_embedding_lookup_op(embed, indices, scale, zero_point, digit, ctx=None):
    return ALPTEmbeddingLookUpOp(embed, indices, scale, zero_point, digit, ctx=ctx)


class ALPTRoundingOp(Op):
    def __init__(self, lookup, scale, middle, digit, ctx=None):
        # lookup is actually w/delta
        super().__init__(ALPTRoundingOp, [lookup, scale], ctx)
        self.digit = digit
        self.middle = middle

    def compute(self, input_vals, output_val, stream_handle=None):
        if self.on_cpu:
            raise NotImplementedError
        else:
            lsq_rounding(input_vals[0], input_vals[1], output_val,
                         self.middle, self.digit, stream_handle)

    def gradient(self, output_grad):
        grad_node = alpt_scale_gradient_op(
            output_grad, self.inputs[0], self.digit, ctx=self.raw_ctx)
        return [None, mul_op(output_grad, grad_node)]

    def infer_shape(self, input_shapes):
        assert len(input_shapes) == 2
        return input_shapes[0]


def alpt_rounding_op(lookup, scale, middle, digit, ctx=None):
    return ALPTRoundingOp(lookup, scale, middle, digit, ctx=ctx)


class ALPTScaleGradientOp(Op):
    def __init__(self, lookup, digit, ctx=None):
        super().__init__(ALPTScaleGradientOp, [lookup], ctx)
        self.digit = digit

    def compute(self, input_vals, output_val, stream_handle=None):
        if self.on_cpu:
            raise NotImplementedError
        else:
            lsq_rounding_gradient(
                input_vals[0], output_val, self.digit, stream_handle)

    def gradient(self, output_grad):
        raise NotImplementedError

    def infer_shape(self, input_shapes):
        assert len(input_shapes) == 1
        return input_shapes[0]


def alpt_scale_gradient_op(lookup, digit, ctx=None):
    return ALPTScaleGradientOp(lookup, digit, ctx=ctx)