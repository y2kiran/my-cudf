# Copyright (c) 2024-2025, NVIDIA CORPORATION.
import datetime

import pyarrow as pa
import pytest

import pylibcudf as plc
from pylibcudf.types import DataType, TypeId


@pytest.fixture(scope="module")
def np():
    return pytest.importorskip("numpy")


@pytest.mark.parametrize(
    "val", [True, False, -1, 0, 1 - 1.0, 0.0, 1.52, "", "a1!"]
)
def test_from_py(val):
    result = plc.Scalar.from_py(val)
    expected = pa.scalar(val)
    assert plc.interop.to_arrow(result).equals(expected)


@pytest.mark.parametrize(
    "val,tid",
    [
        (1, TypeId.INT8),
        (1, TypeId.INT16),
        (1, TypeId.INT32),
        (1, TypeId.INT64),
        (1, TypeId.UINT8),
        (1, TypeId.UINT16),
        (1, TypeId.UINT32),
        (1, TypeId.UINT64),
        (1.0, TypeId.FLOAT32),
        (1.5, TypeId.FLOAT64),
        ("str", TypeId.STRING),
        (True, TypeId.BOOL8),
    ],
)
def test_from_py_with_dtype(val, tid):
    dtype = DataType(tid)
    result = plc.Scalar.from_py(val, dtype)
    expected = pa.scalar(val).cast(plc.interop.to_arrow(dtype))
    assert plc.interop.to_arrow(result).equals(expected)


@pytest.mark.parametrize(
    "val,tid,error,msg",
    [
        (
            -1,
            TypeId.UINT8,
            ValueError,
            "Cannot assign negative value to UINT8 scalar",
        ),
        (
            -1,
            TypeId.UINT16,
            ValueError,
            "Cannot assign negative value to UINT16 scalar",
        ),
        (
            -1,
            TypeId.UINT32,
            ValueError,
            "Cannot assign negative value to UINT32 scalar",
        ),
        (
            -1,
            TypeId.UINT64,
            ValueError,
            "Cannot assign negative value to UINT64 scalar",
        ),
        (
            1,
            TypeId.FLOAT32,
            TypeError,
            "Cannot convert int to Scalar with dtype FLOAT32",
        ),
        (
            1,
            TypeId.FLOAT64,
            TypeError,
            "Cannot convert int to Scalar with dtype FLOAT64",
        ),
        (
            1,
            TypeId.BOOL8,
            TypeError,
            "Cannot convert int to Scalar with dtype BOOL8",
        ),
        (
            "str",
            TypeId.INT32,
            TypeError,
            "Cannot convert str to Scalar with dtype INT32",
        ),
        (
            True,
            TypeId.INT32,
            TypeError,
            "Cannot convert bool to Scalar with dtype INT32",
        ),
        (
            1.5,
            TypeId.INT32,
            TypeError,
            "Cannot convert float to Scalar with dtype INT32",
        ),
    ],
)
def test_from_py_with_dtype_errors(val, tid, error, msg):
    dtype = DataType(tid)
    with pytest.raises(error, match=msg):
        plc.Scalar.from_py(val, dtype)


@pytest.mark.parametrize(
    "val, tid",
    [
        (-(2**7) - 1, TypeId.INT8),
        (2**7, TypeId.INT8),
        (2**15, TypeId.INT16),
        (2**31, TypeId.INT32),
        (2**63, TypeId.INT64),
        (2**8, TypeId.UINT8),
        (2**16, TypeId.UINT16),
        (2**32, TypeId.UINT32),
        (2**64, TypeId.UINT64),
        (float(2**150), TypeId.FLOAT32),
        (float(-(2**150)), TypeId.FLOAT32),
    ],
)
def test_from_py_overflow_errors(val, tid):
    dtype = DataType(tid)
    with pytest.raises(OverflowError, match="out of range"):
        plc.Scalar.from_py(val, dtype)


@pytest.mark.parametrize(
    "val", [datetime.datetime(2020, 1, 1), datetime.timedelta(1), [1], {1: 1}]
)
def test_from_py_notimplemented(val):
    with pytest.raises(NotImplementedError):
        plc.Scalar.from_py(val)


@pytest.mark.parametrize("val", [object, None])
def test_from_py_typeerror(val):
    with pytest.raises(TypeError):
        plc.Scalar.from_py(val)


@pytest.mark.parametrize(
    "np_type",
    [
        "bool_",
        "str_",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "float32",
        "float64",
    ],
)
def test_from_numpy(np, np_type):
    np_klass = getattr(np, np_type)
    np_val = np_klass("1" if np_type == "str_" else 1)
    result = plc.Scalar.from_numpy(np_val)
    expected = pa.scalar(np_val)
    assert plc.interop.to_arrow(result).equals(expected)


@pytest.mark.parametrize("np_type", ["datetime64", "timedelta64"])
def test_from_numpy_notimplemented(np, np_type):
    np_val = getattr(np, np_type)(1, "ns")
    with pytest.raises(NotImplementedError):
        plc.Scalar.from_numpy(np_val)


def test_from_numpy_typeerror(np):
    with pytest.raises(TypeError):
        plc.Scalar.from_numpy(np.void(5))
