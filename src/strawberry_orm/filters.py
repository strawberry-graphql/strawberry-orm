"""Shared filter lookup types for strawberry-orm.

All lookup types are defined as Strawberry input types. Each backend translates
these into native ORM filter expressions (Django Q objects, SQLAlchemy
whereclause, Tortoise Q objects).
"""

from __future__ import annotations

import datetime
import re
import warnings
from decimal import Decimal
from typing import Any

import strawberry

#: The longest pattern a ``regex`` or ``iRegex`` filter takes.
MAX_REGEX_LENGTH = 100


def check_regex(pattern: str) -> None:
    """Refuse a pattern whose matching can take more than quadratic time.

    SQLite matches a regex with Python's ``re``, in the server's process and
    holding the GIL, so one such pattern stalls every request the process
    serves. A repeat that holds a repeat of no fixed length, or a choice
    between alternatives, as in ``(a+)+`` or ``(a|aa)+``, backtracks through
    every way of splitting the value; a back-reference can as well. So do
    repeats of no fixed length in a row that can match the same characters,
    as in ``.*.*.*``: each one multiplies the time by the value's length. What
    is left costs at most time quadratic in the value's length per row. A
    pattern Python cannot parse is left to the database, which refuses it or
    reads it in a dialect of its own.
    """
    from re import _constants as sre
    from re import _parser

    if len(pattern) > MAX_REGEX_LENGTH:
        raise ValueError(f"A regex filter takes at most {MAX_REGEX_LENGTH} characters.")
    try:
        with warnings.catch_warnings():
            # PostgreSQL's ``[[:alpha:]]`` reads to Python as a nested set.
            warnings.simplefilter("ignore")
            parsed = _parser.parse(pattern)
    except (re.error, RecursionError, OverflowError):
        return
    if _backtracks(parsed, sre, repeated=False):
        raise ValueError(
            "A regex filter may not repeat a group that repeats or offers a "
            "choice, nor refer back to a group."
        )
    if _Trades(parsed, sre).found():
        raise ValueError(
            "A regex filter may not hold two repeats of variable length that "
            "can match the same characters, as in '.*.*' or 'a*a+', unless a "
            "character the first cannot match is required between them. A "
            "regex matches anywhere in the value, so a leading or trailing "
            "'.*' is never needed."
        )


#: Characters an overlap between two character sets is looked for in, beside
#: every literal and range end the pattern names.
_SAMPLE = frozenset(
    [chr(code) for code in range(0x250)]
    + ["\u00a0", "\u0660", "\u2028", "\u3000", "\u4e00", "\U0001f600"]
)


class _Trades:
    """Whether two variable-length repeats can split a value between them.

    Python's ``re`` tries every way of dividing a run of characters between
    repeats that can each match them, so *k* such repeats in a row cost time
    of the order of the value's length to the power *k*. A repeat stays open
    until the pattern requires a character it cannot match; a second
    variable-length repeat overlapping one still open is refused. Possessive
    repeats and atomic groups give nothing back, so they open nothing. Letters
    are compared without regard to case, since ``iRegex`` ignores it.
    """

    def __init__(self, parsed: Any, sre: Any) -> None:
        self.sre = sre
        self.state = parsed.state
        self.sample = set(_SAMPLE) | self._named(parsed)
        self.parsed = parsed
        self.literals: dict[int, set[str]] = {}

    def found(self) -> bool:
        try:
            self._walk(self.parsed, [])
        except _TradeFound:
            return True
        return False

    def _named(self, pattern: Any) -> set[str]:
        sre, named = self.sre, set()
        for op, av in pattern:
            if op in (sre.LITERAL, sre.NOT_LITERAL):
                named.add(chr(av))
            elif op is sre.RANGE:
                named.update((chr(av[0]), chr(av[1])))
            elif op is sre.IN:
                named |= self._named(av)
            elif op is sre.SUBPATTERN:
                named |= self._named(av[-1])
            elif op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                named |= self._named(av[2])
            elif op is sre.BRANCH:
                for branch in av[1]:
                    named |= self._named(branch)
            elif op in (sre.ASSERT, sre.ASSERT_NOT):
                named |= self._named(av[1])
            elif op is sre.ATOMIC_GROUP:
                named |= self._named(av)
        return {variant for ch in named for variant in _cases(ch)}

    def _walk(self, pattern: Any, open_sets: list[frozenset[str]]) -> list:
        sre = self.sre
        for op, av in pattern:
            if op is sre.AT:
                continue
            if op in (sre.ASSERT, sre.ASSERT_NOT):
                self._walk(av[1], list(open_sets))
                continue
            if op is sre.SUBPATTERN:
                open_sets = self._walk(av[-1], open_sets)
                continue
            if op is sre.BRANCH:
                after: list[frozenset[str]] = []
                for branch in av[1]:
                    after.extend(self._walk(branch, list(open_sets)))
                open_sets = after
                continue
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
                low, high, body = av
                if low == high == 1:
                    open_sets = self._walk(body, open_sets)
                    continue
                if low != high:
                    self._walk(body, list(open_sets))
                    chars = self._chars(body)
                    if any(chars & before for before in open_sets):
                        raise _TradeFound
                    required = low > 0 and self._width(body) > 0
                    open_sets = [chars] if required else [*open_sets, chars]
                    continue
            elif op is sre.POSSESSIVE_REPEAT:
                self._walk(av[2], list(open_sets))
            elif op is sre.ATOMIC_GROUP:
                self._walk(av, list(open_sets))
            if self._width([(op, av)]) > 0:
                chars = self._chars([(op, av)])
                open_sets = [before for before in open_sets if before & chars]
        return open_sets

    def _width(self, items: list) -> int:
        from re import _parser

        return _parser.SubPattern(self.state, items).getwidth()[0]

    def _chars(self, pattern: Any) -> frozenset[str]:
        """Every sampled character something in *pattern* can consume."""
        sre, found = self.sre, set()
        for op, av in pattern:
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                found |= self._chars(av[2])
            elif op is sre.SUBPATTERN:
                found |= self._chars(av[-1])
            elif op is sre.ATOMIC_GROUP:
                found |= self._chars(av)
            elif op is sre.BRANCH:
                for branch in av[1]:
                    found |= self._chars(branch)
            elif op not in (sre.AT, sre.ASSERT, sre.ASSERT_NOT):
                found |= self._atom(op, av)
        return frozenset(found)

    def _atom(self, op: Any, av: Any) -> set[str]:
        sre, sample = self.sre, self.sample
        if op is sre.LITERAL:
            if av not in self.literals:
                self.literals[av] = {ch for ch in sample if _same_letter(ch, chr(av))}
            return self.literals[av]
        if op is sre.NOT_LITERAL:
            return sample - {chr(av)}
        if op is sre.IN:
            negated = bool(av) and av[0][0] is sre.NEGATE
            items = av[1:] if negated else av
            if not negated:
                return set().union(*(self._atom(*item) for item in items))
            # A smaller set negated is the larger, so match the listed ones exactly.
            listed = {
                ch for ch in sample if any(self._exactly(item, ch) for item in items)
            }
            return sample - listed
        if op is sre.RANGE:
            low, high = av
            return {
                ch for ch in sample if any(low <= ord(v) <= high for v in _cases(ch))
            }
        if op is sre.CATEGORY:
            return {ch for ch in sample if _in_category(sre, av, ch)}
        return set(sample)

    def _exactly(self, item: Any, ch: str) -> bool:
        sre = self.sre
        op, av = item
        if op is sre.LITERAL:
            return ch == chr(av)
        if op is sre.RANGE:
            return av[0] <= ord(ch) <= av[1]
        return op is sre.CATEGORY and _in_category(sre, av, ch)


class _TradeFound(Exception):
    pass


def _cases(ch: str) -> set[str]:
    return {ch} | {v for v in (ch.lower(), ch.upper()) if len(v) == 1}


def _same_letter(ch: str, other: str) -> bool:
    return bool(_cases(ch) & _cases(other))


def _in_category(sre: Any, category: Any, ch: str) -> bool:
    word = ch.isalnum() or ch == "_"
    tests = {
        sre.CATEGORY_DIGIT: ch.isdecimal(),
        sre.CATEGORY_NOT_DIGIT: not ch.isdecimal(),
        sre.CATEGORY_SPACE: ch.isspace(),
        sre.CATEGORY_NOT_SPACE: not ch.isspace(),
        sre.CATEGORY_WORD: word,
        sre.CATEGORY_NOT_WORD: not word,
    }
    return tests.get(category, True)


def _backtracks(pattern: Any, sre: Any, *, repeated: bool) -> bool:
    repeats = (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT)
    for op, av in pattern:
        if op in (sre.GROUPREF, sre.GROUPREF_EXISTS):
            return True
        if op in repeats:
            low, high, inner = av
            if (repeated and low != high) or _backtracks(
                inner, sre, repeated=repeated or high > 1
            ):
                return True
        elif op is sre.BRANCH:
            if repeated or any(
                _backtracks(branch, sre, repeated=False) for branch in av[1]
            ):
                return True
        elif op is sre.SUBPATTERN:
            if _backtracks(av[-1], sre, repeated=repeated):
                return True
        elif op in (sre.ASSERT, sre.ASSERT_NOT):
            if _backtracks(av[1], sre, repeated=repeated):
                return True
        elif op is sre.ATOMIC_GROUP and _backtracks(av, sre, repeated=repeated):
            return True
    return False


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


@strawberry.input
class ReferenceLookup:
    """Filter by primary-key values (no join when used via ``object.<relation>``).

    Exposed on a model's own PK fields (e.g. ``id``, ``publisher_code``).
    Filtering a parent by a related PK uses ``object.<relation>.field.<pk>``,
    which applies the parent's forward FK column without joining.
    """

    exact: strawberry.ID | None = strawberry.UNSET
    neq: strawberry.ID | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[strawberry.ID] | None = strawberry.UNSET
    not_in_list: list[strawberry.ID] | None = strawberry.UNSET


def is_reference_lookup(lookup: Any) -> bool:
    """Return True when *lookup* is a :class:`ReferenceLookup` input instance."""
    return isinstance(lookup, ReferenceLookup)


_FK_SHORTCUT_INT_OPS = frozenset({"exact", "neq", "is_null", "in_list", "not_in_list"})


def is_fk_shortcut_lookup(lookup: Any) -> bool:
    """Return True when *lookup* can target a parent FK column without joining."""
    if is_reference_lookup(lookup):
        return True
    if not isinstance(lookup, IntComparisonLookup):
        return False
    has_op = False
    for op_name in lookup.__class__.__dataclass_fields__:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue
        has_op = True
        if op_name not in _FK_SHORTCUT_INT_OPS:
            return False
    return has_op


def _unwrap_optional_lookup_type(lookup_type: Any) -> Any:
    of_type = getattr(lookup_type, "of_type", None)
    if of_type is not None:
        return _unwrap_optional_lookup_type(of_type)
    annotation = getattr(lookup_type, "annotation", None)
    if annotation is not None:
        return annotation
    return lookup_type


def is_reference_lookup_type(lookup_type: Any) -> bool:
    """Return True when *lookup_type* is :class:`ReferenceLookup` (or optional)."""
    inner = _unwrap_optional_lookup_type(lookup_type)
    if inner is ReferenceLookup:
        return True
    origin = getattr(inner, "__origin__", None)
    args = getattr(inner, "__args__", ())
    if origin is ReferenceLookup:
        return True
    return any(arg is ReferenceLookup for arg in args if arg is not type(None))


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
