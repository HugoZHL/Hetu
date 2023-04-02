from __future__ import absolute_import

import ctypes
from .._base import _LIB
from .. import ndarray as _nd


def tensor_quantize(in_arr, out_arr, digit, scale, zero_point, stream=None):
    assert isinstance(in_arr, _nd.NDArray)
    assert isinstance(out_arr, _nd.NDArray)
    _LIB.DLGpuQuantize(in_arr.handle, out_arr.handle, ctypes.c_int(digit), ctypes.c_float(
        scale), ctypes.c_int64(zero_point), stream.handle if stream else None)


def tensor_dequantize(in_arr, out_arr, digit, scale, zero_point, stream=None):
    assert isinstance(in_arr, _nd.NDArray)
    assert isinstance(out_arr, _nd.NDArray)
    _LIB.DLGpuDequantize(in_arr.handle, out_arr.handle, ctypes.c_int(digit), ctypes.c_float(
        scale), ctypes.c_int64(zero_point), stream.handle if stream else None)