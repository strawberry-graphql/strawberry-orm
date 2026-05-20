"""Shared filter lookup types for strawberry-orm.

All lookup types are defined as Strawberry input types. Each backend translates
these into native ORM filter expressions (Django Q objects, SQLAlchemy
whereclause, Tortoise Q objects).
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import strawberry

# ---------------------------------------------------------------------------
# Base lookups
# ---------------------------------------------------------------------------


@strawberry.input
class StringLookup:
    exact: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[str] | None = strawberry.UNSET
    not_in_list: list[str] | None = strawberry.UNSET
    contains: str | None = strawberry.UNSET
    i_contains: str | None = strawberry.UNSET
    starts_with: str | None = strawberry.UNSET
    i_starts_with: str | None = strawberry.UNSET
    ends_with: str | None = strawberry.UNSET
    i_ends_with: str | None = strawberry.UNSET
    regex: str | None = strawberry.UNSET
    i_regex: str | None = strawberry.UNSET


@strawberry.input
class StringLookupNoRegex:
    """StringLookup variant that omits regex/i_regex from the GraphQL schema."""

    exact: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[str] | None = strawberry.UNSET
    not_in_list: list[str] | None = strawberry.UNSET
    contains: str | None = strawberry.UNSET
    i_contains: str | None = strawberry.UNSET
    starts_with: str | None = strawberry.UNSET
    i_starts_with: str | None = strawberry.UNSET
    ends_with: str | None = strawberry.UNSET
    i_ends_with: str | None = strawberry.UNSET


@strawberry.input
class BooleanLookup:
    exact: bool | None = strawberry.UNSET
    neq: bool | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input
class IDLookup:
    exact: strawberry.ID | None = strawberry.UNSET
    neq: strawberry.ID | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[strawberry.ID] | None = strawberry.UNSET
    not_in_list: list[strawberry.ID] | None = strawberry.UNSET


# ---------------------------------------------------------------------------
# Comparison lookups (numeric, date, time, datetime)
# ---------------------------------------------------------------------------


@strawberry.input
class IntRangeInput:
    start: int
    end: int


@strawberry.input
class IntComparisonLookup:
    exact: int | None = strawberry.UNSET
    neq: int | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[int] | None = strawberry.UNSET
    not_in_list: list[int] | None = strawberry.UNSET
    gt: int | None = strawberry.UNSET
    gte: int | None = strawberry.UNSET
    lt: int | None = strawberry.UNSET
    lte: int | None = strawberry.UNSET
    range: IntRangeInput | None = strawberry.UNSET


@strawberry.input
class FloatRangeInput:
    start: float
    end: float


@strawberry.input
class FloatComparisonLookup:
    exact: float | None = strawberry.UNSET
    neq: float | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[float] | None = strawberry.UNSET
    not_in_list: list[float] | None = strawberry.UNSET
    gt: float | None = strawberry.UNSET
    gte: float | None = strawberry.UNSET
    lt: float | None = strawberry.UNSET
    lte: float | None = strawberry.UNSET
    range: FloatRangeInput | None = strawberry.UNSET


# ---------------------------------------------------------------------------
# Date / time lookups
# ---------------------------------------------------------------------------


@strawberry.input
class DateRangeInput:
    start: str
    end: str


@strawberry.input
class DateComparisonLookup:
    exact: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[str] | None = strawberry.UNSET
    not_in_list: list[str] | None = strawberry.UNSET
    gt: str | None = strawberry.UNSET
    gte: str | None = strawberry.UNSET
    lt: str | None = strawberry.UNSET
    lte: str | None = strawberry.UNSET
    range: DateRangeInput | None = strawberry.UNSET


@strawberry.input
class TimeRangeInput:
    start: str
    end: str


@strawberry.input
class TimeComparisonLookup:
    exact: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    gt: str | None = strawberry.UNSET
    gte: str | None = strawberry.UNSET
    lt: str | None = strawberry.UNSET
    lte: str | None = strawberry.UNSET
    range: TimeRangeInput | None = strawberry.UNSET


@strawberry.input
class DateTimeRangeInput:
    start: str
    end: str


@strawberry.input
class DateTimeComparisonLookup:
    exact: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[str] | None = strawberry.UNSET
    not_in_list: list[str] | None = strawberry.UNSET
    gt: str | None = strawberry.UNSET
    gte: str | None = strawberry.UNSET
    lt: str | None = strawberry.UNSET
    lte: str | None = strawberry.UNSET
    range: DateTimeRangeInput | None = strawberry.UNSET


# ---------------------------------------------------------------------------
# Type-to-lookup mapping (used by backends during code generation)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Custom filter / order field decorators
# ---------------------------------------------------------------------------

_CUSTOM_FILTER_ATTR = "_orm_custom_filter"
_CUSTOM_ORDER_ATTR = "_orm_custom_order"
_CUSTOM_GROUP_ATTR = "_orm_custom_group"
_CUSTOM_AGGREGATE_ATTR = "_orm_custom_aggregate"


def filter_field(func):
    """Mark a method as a custom filter handler.

    The decorated method must accept ``(self, value: <type>, query)`` and
    return the modified query object.  An optional ``info`` parameter will
    receive the Strawberry ``Info`` when present in the signature.

    Usage inside an ``@orm.filter_type(Model)`` class::

        @orm.filter_type(User)
        class UserFilter:
            name: auto

            @filter_field
            def search(self, value: str, query):
                return query.where(...)
    """
    func._orm_custom_filter = True
    return func


def order_field(func):
    """Mark a method as a custom order handler.

    The decorated method must accept ``(self, value: Ordering, query)`` and
    return the modified query object.  An optional ``info`` parameter will
    receive the Strawberry ``Info`` when present in the signature.

    Usage inside an ``@orm.order_type(Model)`` class::

        @orm.order_type(User)
        class UserOrder:
            name: auto

            @order_field
            def post_count(self, value: Ordering, query):
                ...
    """
    func._orm_custom_order = True
    return func


def group_field(func):
    """Mark a method as a custom group-by handler.

    The decorated method must accept ``(self, value: bool, query)`` and
    return a column expression (or tuple of column expression + label)
    to use in the GROUP BY clause.  An optional ``info`` parameter will
    receive the Strawberry ``Info`` when present in the signature.

    Usage inside an ``@orm.group_type(Model)`` class::

        @orm.group_type(Order)
        class OrderGroupBy:
            status: auto

            @group_field
            def by_customer_tier(self, value: bool, query):
                ...
    """
    func._orm_custom_group = True
    return func


def aggregate_field(func):
    """Mark a method as a custom aggregate handler.

    The decorated method must accept ``(self, columns)`` and return a
    SQLAlchemy column expression.  ``columns`` is the subquery's column
    collection (``subq.c``).  An optional ``info`` parameter will receive
    the Strawberry ``Info`` when present in the signature.

    The method's return-type annotation determines the GraphQL scalar
    type of the aggregate field (defaults to ``float | None``).

    Usage inside an ``@orm.aggregate_type(Order)`` class::

        @orm.aggregate_type(Order)
        class OrderAggregation:
            amount: auto
            quantity: auto

            @aggregate_field
            def total_revenue(self, columns) -> float:
                from sqlalchemy import func
                return func.sum(columns.amount * columns.quantity)
    """
    func._orm_custom_aggregate = True
    return func
