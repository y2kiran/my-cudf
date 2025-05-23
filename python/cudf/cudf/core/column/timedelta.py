# Copyright (c) 2020-2025, NVIDIA CORPORATION.

from __future__ import annotations

import functools
import math
from typing import TYPE_CHECKING, Any, cast

import cupy as cp
import numpy as np
import pandas as pd
import pyarrow as pa

import pylibcudf as plc

import cudf
import cudf.core.column.column as column
from cudf.api.types import is_scalar
from cudf.core._internals import binaryop
from cudf.core.buffer import Buffer, acquire_spill_lock
from cudf.core.column.column import ColumnBase
from cudf.core.scalar import pa_scalar_to_plc_scalar
from cudf.utils.dtypes import (
    CUDF_STRING_DTYPE,
    cudf_dtype_from_pa_type,
    cudf_dtype_to_pa_type,
    find_common_type,
)
from cudf.utils.utils import (
    _all_bools_with_nulls,
    _datetime_timedelta_find_and_replace,
    is_na_like,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cudf._typing import (
        ColumnBinaryOperand,
        ColumnLike,
        DatetimeLikeScalar,
        DtypeObj,
        ScalarLike,
    )

_unit_to_nanoseconds_conversion = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
    "D": 86_400_000_000_000,
}


@functools.cache
def get_np_td_unit_conversion(
    reso: str, dtype: None | np.dtype
) -> np.timedelta64:
    td = np.timedelta64(_unit_to_nanoseconds_conversion[reso], "ns")
    if dtype is not None:
        return td.astype(dtype)
    return td


class TimeDeltaColumn(ColumnBase):
    """
    Parameters
    ----------
    data : Buffer
        The Timedelta values
    dtype : np.dtype
        The data type
    size : int
        Size of memory allocation.
    mask : Buffer; optional
        The validity mask
    offset : int
        Data offset
    null_count : int, optional
        The number of null values.
        If None, it is calculated automatically.
    """

    _VALID_BINARY_OPERATIONS = {
        "__eq__",
        "__ne__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
        "__add__",
        "__sub__",
        "__mul__",
        "__mod__",
        "__truediv__",
        "__floordiv__",
        "__radd__",
        "__rsub__",
        "__rmul__",
        "__rmod__",
        "__rtruediv__",
        "__rfloordiv__",
    }

    _PANDAS_NA_REPR = str(pd.NaT)

    def __init__(
        self,
        data: Buffer,
        size: int | None,
        dtype: np.dtype,
        mask: Buffer | None = None,
        offset: int = 0,
        null_count: int | None = None,
        children: tuple = (),
    ):
        if not isinstance(data, Buffer):
            raise ValueError("data must be a Buffer.")
        if not (isinstance(dtype, np.dtype) and dtype.kind == "m"):
            raise ValueError("dtype must be a timedelta numpy dtype.")

        if data.size % dtype.itemsize:
            raise ValueError("Buffer size must be divisible by element size")
        if size is None:
            size = data.size // dtype.itemsize
            size = size - offset
        if len(children) != 0:
            raise ValueError("TimeDeltaColumn must have no children.")
        super().__init__(
            data=data,
            size=size,
            dtype=dtype,
            mask=mask,
            offset=offset,
            null_count=null_count,
            children=children,
        )

    def __contains__(self, item: DatetimeLikeScalar) -> bool:
        try:
            item = np.timedelta64(item, self.time_unit)
        except ValueError:
            # If item cannot be converted to duration type
            # np.timedelta64 raises ValueError, hence `item`
            # cannot exist in `self`.
            return False
        return item.view(np.dtype(np.int64)) in cast(
            "cudf.core.column.NumericalColumn", self.astype(np.dtype(np.int64))
        )

    def _validate_fillna_value(
        self, fill_value: ScalarLike | ColumnLike
    ) -> plc.Scalar | ColumnBase:
        """Align fill_value for .fillna based on column type."""
        if (
            isinstance(fill_value, np.timedelta64)
            and self.time_unit != np.datetime_data(fill_value)[0]
        ):
            fill_value = fill_value.astype(self.dtype)
        elif isinstance(fill_value, str) and fill_value.lower() == "nat":
            fill_value = np.timedelta64(fill_value, self.time_unit)
        return super()._validate_fillna_value(fill_value)

    @property
    def values(self):
        """
        Return a CuPy representation of the TimeDeltaColumn.
        """
        raise NotImplementedError(
            "TimeDelta Arrays is not yet implemented in cudf"
        )

    def element_indexing(self, index: int):
        result = super().element_indexing(index)
        if cudf.get_option("mode.pandas_compatible"):
            return pd.Timedelta(result)
        return result

    def to_pandas(
        self,
        *,
        nullable: bool = False,
        arrow_type: bool = False,
    ) -> pd.Index:
        if arrow_type and nullable:
            raise ValueError(
                f"{arrow_type=} and {nullable=} cannot both be set."
            )
        elif nullable:
            raise NotImplementedError(f"{nullable=} is not implemented.")
        pa_array = self.to_arrow()
        if arrow_type:
            return pd.Index(pd.arrays.ArrowExtensionArray(pa_array))
        else:
            # Workaround for timedelta types until the following issue is fixed:
            # https://github.com/apache/arrow/issues/45341
            return pd.Index(
                pa_array.to_numpy(zero_copy_only=False, writable=True)
            )

    def _binaryop(self, other: ColumnBinaryOperand, op: str) -> ColumnBase:
        reflect, op = self._check_reflected_op(op)
        other = self._normalize_binop_operand(other)
        if other is NotImplemented:
            return NotImplemented

        this: ColumnBinaryOperand = self
        out_dtype = None
        other_cudf_dtype = (
            cudf_dtype_from_pa_type(other.type)
            if isinstance(other, pa.Scalar)
            else other.dtype
        )

        if other_cudf_dtype.kind == "m":
            # TODO: pandas will allow these operators to work but return false
            # when comparing to non-timedelta dtypes. We should do the same.
            if op in {
                "__eq__",
                "__ne__",
                "__lt__",
                "__gt__",
                "__le__",
                "__ge__",
                "NULL_EQUALS",
                "NULL_NOT_EQUALS",
            }:
                out_dtype = np.dtype(np.bool_)
            elif op == "__mod__":
                out_dtype = find_common_type((self.dtype, other_cudf_dtype))
            elif op in {"__truediv__", "__floordiv__"}:
                common_dtype = find_common_type((self.dtype, other_cudf_dtype))
                out_dtype = (
                    np.dtype(np.float64)
                    if op == "__truediv__"
                    else np.dtype(np.int64)
                )
                this = self.astype(common_dtype).astype(out_dtype)
                if isinstance(other, pa.Scalar):
                    if other.is_valid:
                        # pyarrow.cast doesn't support casting duration to float
                        # so go through numpy
                        other_np = pa.array([other]).to_numpy(
                            zero_copy_only=False
                        )
                        other_np = other_np.astype(common_dtype).astype(
                            out_dtype
                        )
                        other = pa.array(other_np)[0]
                    else:
                        other = pa.scalar(
                            None, type=cudf_dtype_to_pa_type(out_dtype)
                        )
                else:
                    other = other.astype(common_dtype).astype(out_dtype)
            elif op in {"__add__", "__sub__"}:
                out_dtype = find_common_type((self.dtype, other_cudf_dtype))
        elif other_cudf_dtype.kind in {"f", "i", "u"}:
            if op in {"__mul__", "__mod__", "__truediv__", "__floordiv__"}:
                out_dtype = self.dtype
            elif op in {"__eq__", "__ne__", "NULL_EQUALS", "NULL_NOT_EQUALS"}:
                if isinstance(other, ColumnBase) and not isinstance(
                    other, TimeDeltaColumn
                ):
                    fill_value = op in ("__ne__", "NULL_NOT_EQUALS")
                    result = _all_bools_with_nulls(
                        self,
                        other,
                        bool_fill_value=fill_value,
                    )
                    if cudf.get_option("mode.pandas_compatible"):
                        result = result.fillna(fill_value)
                    return result

        if out_dtype is None:
            return NotImplemented
        elif isinstance(other, pa.Scalar):
            other = pa_scalar_to_plc_scalar(other)

        lhs, rhs = (other, this) if reflect else (this, other)

        result = binaryop.binaryop(lhs, rhs, op, out_dtype)
        if cudf.get_option("mode.pandas_compatible") and out_dtype.kind == "b":
            result = result.fillna(op == "__ne__")
        return result

    def _normalize_binop_operand(self, other: Any) -> pa.Scalar | ColumnBase:
        if isinstance(other, ColumnBase):
            return other
        elif isinstance(other, (cp.ndarray, np.ndarray)) and other.ndim == 0:
            other = other[()]

        if is_scalar(other):
            if is_na_like(other):
                return super()._normalize_binop_operand(other)
            elif isinstance(other, pd.Timedelta):
                other = other.to_numpy()
            elif isinstance(other, (np.datetime64, np.timedelta64)):
                unit = np.datetime_data(other)[0]
                if unit not in {"s", "ms", "us", "ns"}:
                    if np.isnat(other):
                        # TODO: Use self.time_unit to not modify the result resolution?
                        to_unit = "ns"
                    else:
                        to_unit = self.time_unit
                    if np.isnat(other):
                        # Workaround for https://github.com/numpy/numpy/issues/28496
                        # Once fixed, can always use the astype below
                        other = type(other)("NaT", to_unit)
                    else:
                        other = other.astype(
                            np.dtype(f"{other.dtype.kind}8[{to_unit}]")
                        )
            scalar = pa.scalar(other)
            if (
                pa.types.is_timestamp(scalar.type)
                and scalar.type.tz is not None
            ):
                raise NotImplementedError(
                    "Binary operations with timezone aware operands is not supported."
                )
            elif pa.types.is_duration(scalar.type):
                common_dtype = find_common_type(
                    (self.dtype, cudf_dtype_from_pa_type(scalar.type))
                )
                scalar = scalar.cast(cudf_dtype_to_pa_type(common_dtype))
            return scalar
        return NotImplemented

    @functools.cached_property
    def time_unit(self) -> str:
        return np.datetime_data(self.dtype)[0]

    def total_seconds(self) -> ColumnBase:
        conversion = _unit_to_nanoseconds_conversion[self.time_unit] / 1e9
        # Typecast to decimal128 to avoid floating point precision issues
        # https://github.com/rapidsai/cudf/issues/17664
        return (
            (self.astype(np.dtype(np.int64)) * conversion)
            .astype(
                cudf.Decimal128Dtype(cudf.Decimal128Dtype.MAX_PRECISION, 9)
            )
            .round(decimals=abs(int(math.log10(conversion))))
            .astype(np.dtype(np.float64))
        )

    def ceil(self, freq: str) -> ColumnBase:
        raise NotImplementedError("ceil is currently not implemented")

    def floor(self, freq: str) -> ColumnBase:
        raise NotImplementedError("floor is currently not implemented")

    def round(self, freq: str) -> ColumnBase:
        raise NotImplementedError("round is currently not implemented")

    def as_numerical_column(
        self, dtype: np.dtype
    ) -> cudf.core.column.NumericalColumn:
        col = cudf.core.column.NumericalColumn(
            data=self.base_data,  # type: ignore[arg-type]
            dtype=np.dtype(np.int64),
            mask=self.base_mask,
            offset=self.offset,
            size=self.size,
        )
        return cast("cudf.core.column.NumericalColumn", col.astype(dtype))

    def as_datetime_column(self, dtype: np.dtype) -> None:  # type: ignore[override]
        raise TypeError(
            f"cannot astype a timedelta from {self.dtype} to {dtype}"
        )

    def strftime(self, format: str) -> cudf.core.column.StringColumn:
        if len(self) == 0:
            return cast(
                cudf.core.column.StringColumn,
                column.column_empty(0, dtype=CUDF_STRING_DTYPE),
            )
        else:
            with acquire_spill_lock():
                return type(self).from_pylibcudf(  # type: ignore[return-value]
                    plc.strings.convert.convert_durations.from_durations(
                        self.to_pylibcudf(mode="read"), format
                    )
                )

    def as_string_column(self) -> cudf.core.column.StringColumn:
        return self.strftime("%D days %H:%M:%S")

    def as_timedelta_column(self, dtype: np.dtype) -> TimeDeltaColumn:
        if dtype == self.dtype:
            return self
        return self.cast(dtype=dtype)  # type: ignore[return-value]

    def find_and_replace(
        self,
        to_replace: ColumnBase,
        replacement: ColumnBase,
        all_nan: bool = False,
    ) -> TimeDeltaColumn:
        return cast(
            TimeDeltaColumn,
            _datetime_timedelta_find_and_replace(
                original_column=self,
                to_replace=to_replace,
                replacement=replacement,
                all_nan=all_nan,
            ),
        )

    def can_cast_safely(self, to_dtype: DtypeObj) -> bool:
        if to_dtype.kind == "m":
            to_res, _ = np.datetime_data(to_dtype)
            self_res = self.time_unit

            max_int = np.iinfo(np.int64).max

            max_dist = np.timedelta64(
                self.max().astype(np.int64, copy=False), self_res
            )
            min_dist = np.timedelta64(
                self.min().astype(np.int64, copy=False), self_res
            )

            self_delta_dtype = np.timedelta64(0, self_res).dtype

            if max_dist <= np.timedelta64(max_int, to_res).astype(
                self_delta_dtype
            ) and min_dist <= np.timedelta64(max_int, to_res).astype(
                self_delta_dtype
            ):
                return True
            else:
                return False
        elif to_dtype == np.dtype(np.int64) or to_dtype == CUDF_STRING_DTYPE:
            # can safely cast to representation, or string
            return True
        else:
            return False

    def mean(self, skipna=None) -> pd.Timedelta:
        return pd.Timedelta(
            cast(
                "cudf.core.column.NumericalColumn",
                self.astype(np.dtype(np.int64)),
            ).mean(skipna=skipna),
            unit=self.time_unit,
        ).as_unit(self.time_unit)

    def median(self, skipna: bool | None = None) -> pd.Timedelta:
        return pd.Timedelta(
            cast(
                "cudf.core.column.NumericalColumn",
                self.astype(np.dtype(np.int64)),
            ).median(skipna=skipna),
            unit=self.time_unit,
        ).as_unit(self.time_unit)

    def isin(self, values: Sequence) -> ColumnBase:
        return cudf.core.tools.datetimes._isin_datetimelike(self, values)

    def quantile(
        self,
        q: np.ndarray,
        interpolation: str,
        exact: bool,
        return_scalar: bool,
    ) -> ColumnBase:
        result = self.astype(np.dtype(np.int64)).quantile(
            q=q,
            interpolation=interpolation,
            exact=exact,
            return_scalar=return_scalar,
        )
        if return_scalar:
            return pd.Timedelta(result, unit=self.time_unit).as_unit(
                self.time_unit
            )
        return result.astype(self.dtype)

    def sum(
        self,
        skipna: bool | None = None,
        min_count: int = 0,
    ) -> pd.Timedelta:
        return pd.Timedelta(
            # Since sum isn't overridden in Numerical[Base]Column, mypy only
            # sees the signature from Reducible (which doesn't have the extra
            # parameters from ColumnBase._reduce) so we have to ignore this.
            self.astype(np.dtype(np.int64)).sum(  # type: ignore
                skipna=skipna, min_count=min_count
            ),
            unit=self.time_unit,
        ).as_unit(self.time_unit)

    def std(
        self,
        skipna: bool | None = None,
        min_count: int = 0,
        ddof: int = 1,
    ) -> pd.Timedelta:
        return pd.Timedelta(
            cast(
                "cudf.core.column.NumericalColumn",
                self.astype(np.dtype(np.int64)),
            ).std(skipna=skipna, min_count=min_count, ddof=ddof),
            unit=self.time_unit,
        ).as_unit(self.time_unit)

    def cov(self, other: TimeDeltaColumn) -> float:
        if not isinstance(other, TimeDeltaColumn):
            raise TypeError(
                f"cannot perform cov with types {self.dtype}, {other.dtype}"
            )
        return cast(
            "cudf.core.column.NumericalColumn", self.astype(np.dtype(np.int64))
        ).cov(
            cast(
                "cudf.core.column.NumericalColumn",
                other.astype(np.dtype(np.int64)),
            )
        )

    def corr(self, other: TimeDeltaColumn) -> float:
        if not isinstance(other, TimeDeltaColumn):
            raise TypeError(
                f"cannot perform corr with types {self.dtype}, {other.dtype}"
            )
        return cast(
            "cudf.core.column.NumericalColumn", self.astype(np.dtype(np.int64))
        ).corr(
            cast(
                "cudf.core.column.NumericalColumn",
                other.astype(np.dtype(np.int64)),
            )
        )

    def components(self) -> dict[str, ColumnBase]:
        """
        Return a Dataframe of the components of the Timedeltas.

        Returns
        -------
        DataFrame

        Examples
        --------
        >>> s = pd.Series(pd.to_timedelta(np.arange(5), unit='s'))
        >>> s = cudf.Series([12231312123, 1231231231, 1123236768712, 2135656,
        ...     3244334234], dtype='timedelta64[ms]')
        >>> s
        0      141 days 13:35:12.123
        1       14 days 06:00:31.231
        2    13000 days 10:12:48.712
        3        0 days 00:35:35.656
        4       37 days 13:12:14.234
        dtype: timedelta64[ms]
        >>> s.dt.components
            days  hours  minutes  seconds  milliseconds  microseconds  nanoseconds
        0    141     13       35       12           123             0            0
        1     14      6        0       31           231             0            0
        2  13000     10       12       48           712             0            0
        3      0      0       35       35           656             0            0
        4     37     13       12       14           234             0            0
        """
        date_meta = {
            "hours": ["D", "h"],
            "minutes": ["h", "m"],
            "seconds": ["m", "s"],
            "milliseconds": ["s", "ms"],
            "microseconds": ["ms", "us"],
            "nanoseconds": ["us", "ns"],
        }
        data = {"days": self // get_np_td_unit_conversion("D", self.dtype)}
        reached_self_unit = False
        for result_key, (mod_unit, div_unit) in date_meta.items():
            if not reached_self_unit:
                res_col = (
                    self % get_np_td_unit_conversion(mod_unit, self.dtype)
                ) // get_np_td_unit_conversion(div_unit, self.dtype)
                reached_self_unit = self.time_unit == div_unit
            else:
                res_col = column.as_column(
                    0, length=len(self), dtype=np.dtype(np.int64)
                )
                if self.nullable:
                    res_col = res_col.set_mask(self.mask)
            data[result_key] = res_col
        return data

    @property
    def days(self) -> cudf.core.column.NumericalColumn:
        """
        Number of days for each element.

        Returns
        -------
        NumericalColumn
        """
        return self // get_np_td_unit_conversion("D", self.dtype)

    @property
    def seconds(self) -> cudf.core.column.NumericalColumn:
        """
        Number of seconds (>= 0 and less than 1 day).

        Returns
        -------
        NumericalColumn
        """
        # This property must return the number of seconds (>= 0 and
        # less than 1 day) for each element, hence first performing
        # mod operation to remove the number of days and then performing
        # division operation to extract the number of seconds.

        return (
            self % get_np_td_unit_conversion("D", self.dtype)
        ) // get_np_td_unit_conversion("s", None)

    @property
    def microseconds(self) -> cudf.core.column.NumericalColumn:
        """
        Number of microseconds (>= 0 and less than 1 second).

        Returns
        -------
        NumericalColumn
        """
        # This property must return the number of microseconds (>= 0 and
        # less than 1 second) for each element, hence first performing
        # mod operation to remove the number of seconds and then performing
        # division operation to extract the number of microseconds.

        return (
            self % get_np_td_unit_conversion("s", self.dtype)
        ) // get_np_td_unit_conversion("us", None)

    @property
    def nanoseconds(self) -> cudf.core.column.NumericalColumn:
        """
        Return the number of nanoseconds (n), where 0 <= n < 1 microsecond.

        Returns
        -------
        NumericalColumn
        """
        # This property must return the number of nanoseconds (>= 0 and
        # less than 1 microsecond) for each element, hence first performing
        # mod operation to remove the number of microseconds and then
        # performing division operation to extract the number
        # of nanoseconds.

        if self.time_unit != "ns":
            res_col = column.as_column(
                0, length=len(self), dtype=np.dtype(np.int64)
            )
            if self.nullable:
                res_col = res_col.set_mask(self.mask)
            return cast("cudf.core.column.NumericalColumn", res_col)
        return (
            self % get_np_td_unit_conversion("us", None)
        ) // get_np_td_unit_conversion("ns", None)
