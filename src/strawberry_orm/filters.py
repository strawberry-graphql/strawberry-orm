"""Shared filter lookup types for strawberry-orm.

All lookup types are defined as Strawberry input types. Each backend translates
these into native ORM filter expressions (Django Q objects, SQLAlchemy
whereclause, Tortoise Q objects).
"""

from __future__ import annotations

from typing import Optional

import strawberry


# ---------------------------------------------------------------------------
# Base lookups
# ---------------------------------------------------------------------------

@strawberry.input
class StringLookup:
    exact: Optional[str] = strawberry.UNSET
    neq: Optional[str] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[str]] = strawberry.UNSET
    not_in_list: Optional[list[str]] = strawberry.UNSET
    contains: Optional[str] = strawberry.UNSET
    i_contains: Optional[str] = strawberry.UNSET
    starts_with: Optional[str] = strawberry.UNSET
    i_starts_with: Optional[str] = strawberry.UNSET
    ends_with: Optional[str] = strawberry.UNSET
    i_ends_with: Optional[str] = strawberry.UNSET
    regex: Optional[str] = strawberry.UNSET
    i_regex: Optional[str] = strawberry.UNSET


@strawberry.input
class BooleanLookup:
    exact: Optional[bool] = strawberry.UNSET
    neq: Optional[bool] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET


@strawberry.input
class IDLookup:
    exact: Optional[strawberry.ID] = strawberry.UNSET
    neq: Optional[strawberry.ID] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[strawberry.ID]] = strawberry.UNSET
    not_in_list: Optional[list[strawberry.ID]] = strawberry.UNSET


# ---------------------------------------------------------------------------
# Comparison lookups (numeric, date, time, datetime)
# ---------------------------------------------------------------------------

@strawberry.input
class IntRangeInput:
    start: int
    end: int


@strawberry.input
class IntComparisonLookup:
    exact: Optional[int] = strawberry.UNSET
    neq: Optional[int] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[int]] = strawberry.UNSET
    not_in_list: Optional[list[int]] = strawberry.UNSET
    gt: Optional[int] = strawberry.UNSET
    gte: Optional[int] = strawberry.UNSET
    lt: Optional[int] = strawberry.UNSET
    lte: Optional[int] = strawberry.UNSET
    range: Optional[IntRangeInput] = strawberry.UNSET


@strawberry.input
class FloatRangeInput:
    start: float
    end: float


@strawberry.input
class FloatComparisonLookup:
    exact: Optional[float] = strawberry.UNSET
    neq: Optional[float] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[float]] = strawberry.UNSET
    not_in_list: Optional[list[float]] = strawberry.UNSET
    gt: Optional[float] = strawberry.UNSET
    gte: Optional[float] = strawberry.UNSET
    lt: Optional[float] = strawberry.UNSET
    lte: Optional[float] = strawberry.UNSET
    range: Optional[FloatRangeInput] = strawberry.UNSET


# ---------------------------------------------------------------------------
# Date / time lookups
# ---------------------------------------------------------------------------

@strawberry.input
class DateRangeInput:
    start: str
    end: str


@strawberry.input
class DateComparisonLookup:
    exact: Optional[str] = strawberry.UNSET
    neq: Optional[str] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[str]] = strawberry.UNSET
    not_in_list: Optional[list[str]] = strawberry.UNSET
    gt: Optional[str] = strawberry.UNSET
    gte: Optional[str] = strawberry.UNSET
    lt: Optional[str] = strawberry.UNSET
    lte: Optional[str] = strawberry.UNSET
    range: Optional[DateRangeInput] = strawberry.UNSET


@strawberry.input
class TimeRangeInput:
    start: str
    end: str


@strawberry.input
class TimeComparisonLookup:
    exact: Optional[str] = strawberry.UNSET
    neq: Optional[str] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    gt: Optional[str] = strawberry.UNSET
    gte: Optional[str] = strawberry.UNSET
    lt: Optional[str] = strawberry.UNSET
    lte: Optional[str] = strawberry.UNSET
    range: Optional[TimeRangeInput] = strawberry.UNSET


@strawberry.input
class DateTimeRangeInput:
    start: str
    end: str


@strawberry.input
class DateTimeComparisonLookup:
    exact: Optional[str] = strawberry.UNSET
    neq: Optional[str] = strawberry.UNSET
    is_null: Optional[bool] = strawberry.UNSET
    in_list: Optional[list[str]] = strawberry.UNSET
    not_in_list: Optional[list[str]] = strawberry.UNSET
    gt: Optional[str] = strawberry.UNSET
    gte: Optional[str] = strawberry.UNSET
    lt: Optional[str] = strawberry.UNSET
    lte: Optional[str] = strawberry.UNSET
    range: Optional[DateTimeRangeInput] = strawberry.UNSET


# ---------------------------------------------------------------------------
# Type-to-lookup mapping (used by backends during code generation)
# ---------------------------------------------------------------------------

import datetime
from decimal import Decimal

TYPE_TO_LOOKUP: dict[type, type] = {
    str: StringLookup,
    int: IntComparisonLookup,
    float: FloatComparisonLookup,
    Decimal: FloatComparisonLookup,
    bool: BooleanLookup,
    datetime.date: DateComparisonLookup,
    datetime.time: TimeComparisonLookup,
    datetime.datetime: DateTimeComparisonLookup,
}
