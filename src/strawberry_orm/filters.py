"""Shared filter lookup types for strawberry-orm.

All lookup types are defined as Strawberry input types. Each backend translates
these into native ORM filter expressions (Django Q objects, SQLAlchemy
whereclause, Tortoise Q objects).
"""

from __future__ import annotations

import bisect
import datetime
import functools
import re
import sys
import warnings
from collections.abc import Iterable
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
    as in ``.*.*.*``: each one multiplies the time by the value's length, a
    possessive one as in ``.*x*+`` too, since it reads its run again each
    time the one before gives a character back. A counted repeat reads its
    whole count each time, so those after a repeat that can match the same
    characters may read no more than ``MAX_REGEX_LENGTH`` characters in all,
    the most a pattern spelling them out could. What is left costs at most time
    quadratic in the value's length per row. A pattern Python cannot parse is
    left to the database, which refuses it or reads it in a dialect of its
    own.
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
    trades = _Trades(parsed, sre)
    if trades.found():
        raise ValueError(
            "A regex filter may not hold two repeats of variable length that "
            "can match the same characters, as in '.*.*' or 'a*a+', unless a "
            "character the first cannot match is required between them. A "
            "regex matches anywhere in the value, so a leading or trailing "
            "'.*' is never needed."
        )
    if trades.counted > MAX_REGEX_LENGTH:
        raise ValueError(
            "A regex filter's counted repeats may read at most "
            f"{MAX_REGEX_LENGTH} characters in all that a repeat before them "
            "can match too, which '.*x{101}' exceeds."
        )


#: Code points, as sorted ``(first, last)`` ranges that neither overlap nor
#: touch.
_Ranges = tuple[tuple[int, int], ...]

_EVERY: _Ranges = ((0, sys.maxunicode),)


class _Trades:
    """Whether two variable-length repeats can split a value between them.

    Python's ``re`` tries every way of dividing a run of characters between
    repeats that can each match them, so *k* such repeats in a row cost time
    of the order of the value's length to the power *k*. A repeat stays open
    until the pattern requires a character it cannot match; a second
    variable-length repeat overlapping one still open is refused, a
    possessive one included. Possessive repeats and atomic groups give
    nothing back, so they open nothing. ``counted`` adds up what the counted
    repeats overlapping an open repeat read. Sets are compared as ranges of
    code points, ``\\d``, ``\\s`` and ``\\w`` as ``re.ASCII`` reads them where
    it is on, and letters with every case ``re`` matches them to, since
    ``iRegex`` ignores case.
    """

    def __init__(self, parsed: Any, sre: Any) -> None:
        self.sre = sre
        self.state = parsed.state
        self.parsed = parsed
        self.counted = 0

    def found(self) -> bool:
        ascii_only = bool(self.state.flags & self.sre.SRE_FLAG_ASCII)
        try:
            self._walk(self.parsed, [], ascii_only)
        except _TradeFound:
            return True
        return False

    def _walk(
        self, pattern: Any, open_sets: list[_Ranges], ascii_only: bool
    ) -> list[_Ranges]:
        sre = self.sre
        for op, av in pattern:
            if op is sre.AT:
                continue
            if op in (sre.ASSERT, sre.ASSERT_NOT):
                self._walk(av[1], list(open_sets), ascii_only)
                continue
            if op is sre.SUBPATTERN:
                open_sets = self._walk(av[-1], open_sets, self._ascii(av, ascii_only))
                continue
            if op is sre.BRANCH:
                after: list[_Ranges] = []
                for branch in av[1]:
                    after.extend(self._walk(branch, list(open_sets), ascii_only))
                open_sets = list(dict.fromkeys(after))
                continue
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                open_sets = self._repeat(op, av, open_sets, ascii_only)
                continue
            if op is sre.ATOMIC_GROUP:
                self._walk(av, list(open_sets), ascii_only)
            if open_sets and self._width([(op, av)]) > 0:
                chars = self._chars([(op, av)], ascii_only)
                open_sets = [before for before in open_sets if _meet(before, chars)]
        return open_sets

    def _repeat(
        self, op: Any, av: Any, open_sets: list[_Ranges], ascii_only: bool
    ) -> list[_Ranges]:
        low, high, body = av
        possessive = op is self.sre.POSSESSIVE_REPEAT
        if low == high == 1 and not possessive:
            return self._walk(body, open_sets, ascii_only)
        if low != high or possessive:
            self._walk(body, list(open_sets), ascii_only)
        chars = self._chars(body, ascii_only)
        overlapping = [before for before in open_sets if _meet(before, chars)]
        if overlapping and low != high:
            raise _TradeFound
        width = low * self._width(body)
        if overlapping and high > 1:
            self.counted += high * self._reads(body)
        if low != high and not possessive:
            return [chars] if width else [*open_sets, chars]
        return overlapping if width else open_sets

    def _ascii(self, av: Any, ascii_only: bool) -> bool:
        """Whether ``re.ASCII`` is on inside the group *av* describes."""
        sre, add_flags = self.sre, av[1]
        if add_flags & (sre.SRE_FLAG_ASCII | sre.SRE_FLAG_UNICODE):
            return bool(add_flags & sre.SRE_FLAG_ASCII)
        return ascii_only

    def _width(self, items: list) -> int:
        from re import _parser

        return _parser.SubPattern(self.state, items).getwidth()[0]

    def _reads(self, pattern: Any) -> int:
        """How many characters matching *pattern* reads, lookarounds included.

        Only for what a counted repeat holds, which ``_backtracks`` leaves no
        choice and no repeat of variable count.
        """
        sre, total = self.sre, 0
        for op, av in pattern:
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                total += av[1] * self._reads(av[2])
            elif op is sre.SUBPATTERN:
                total += self._reads(av[-1])
            elif op is sre.ATOMIC_GROUP:
                total += self._reads(av)
            elif op in (sre.ASSERT, sre.ASSERT_NOT):
                total += self._reads(av[1])
            elif op is not sre.AT:
                total += 1
        return total

    def _chars(self, pattern: Any, ascii_only: bool) -> _Ranges:
        """Every code point something in *pattern* can consume."""
        sre, found = self.sre, []
        for op, av in pattern:
            if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
                found.extend(self._chars(av[2], ascii_only))
            elif op is sre.SUBPATTERN:
                found.extend(self._chars(av[-1], self._ascii(av, ascii_only)))
            elif op is sre.ATOMIC_GROUP:
                found.extend(self._chars(av, ascii_only))
            elif op is sre.BRANCH:
                for branch in av[1]:
                    found.extend(self._chars(branch, ascii_only))
            elif op not in (sre.AT, sre.ASSERT, sre.ASSERT_NOT):
                found.extend(self._atom(op, av, ascii_only))
        return _span(found)

    def _atom(self, op: Any, av: Any, ascii_only: bool, fold: bool = True) -> _Ranges:
        """The code points one character *op* matches, with the other cases
        of a letter it names unless *fold* is false."""
        sre = self.sre
        if op is sre.LITERAL:
            named: _Ranges = ((av, av),)
        elif op is sre.RANGE:
            named = (av,)
        elif op is sre.NOT_LITERAL:
            return _invert(((av, av),))
        elif op is sre.CATEGORY:
            return _categories().get((av, ascii_only), _EVERY if fold else ())
        elif op is sre.IN and av and av[0][0] is sre.NEGATE:
            # A smaller set negated is the larger, so match the listed ones exactly.
            return _invert(
                _span(
                    pair
                    for item in av[1:]
                    for pair in self._atom(*item, ascii_only, fold=False)
                )
            )
        elif op is sre.IN:
            return _span(pair for item in av for pair in self._atom(*item, ascii_only))
        else:
            return _EVERY
        return _either_case(named) if fold else named


class _TradeFound(Exception):
    pass


def _span(ranges: Iterable[tuple[int, int]]) -> _Ranges:
    """*ranges* sorted, with those that overlap or touch joined."""
    joined: list[list[int]] = []
    for low, high in sorted(ranges):
        if joined and low <= joined[-1][1] + 1:
            joined[-1][1] = max(joined[-1][1], high)
        else:
            joined.append([low, high])
    return tuple((low, high) for low, high in joined)


def _invert(ranges: _Ranges) -> _Ranges:
    """Every code point *ranges* leaves out."""
    gaps, start = [], 0
    for low, high in ranges:
        if low > start:
            gaps.append((start, low - 1))
        start = high + 1
    if start <= sys.maxunicode:
        gaps.append((start, sys.maxunicode))
    return tuple(gaps)


def _meet(ranges: _Ranges, others: _Ranges) -> bool:
    """Whether *ranges* and *others* share a code point."""
    i = j = 0
    while i < len(ranges) and j < len(others):
        if ranges[i][1] < others[j][0]:
            i += 1
        elif others[j][1] < ranges[i][0]:
            j += 1
        else:
            return True
    return False


@functools.cache
def _categories() -> dict[tuple[Any, bool], _Ranges]:
    """What ``\\d``, ``\\s``, ``\\w`` and their negations match, by category
    and whether ``re.ASCII`` is on.

    ``re`` folds a character's case before testing it against one, but no
    character's other cases fall outside its category, so they stand as they
    are.
    """
    from re import _constants as sre

    every = "".join(map(chr, range(sys.maxunicode + 1)))
    found: dict[tuple[Any, bool], _Ranges] = {}
    for escape, category, negated in (
        ("\\d", sre.CATEGORY_DIGIT, sre.CATEGORY_NOT_DIGIT),
        ("\\s", sre.CATEGORY_SPACE, sre.CATEGORY_NOT_SPACE),
        ("\\w", sre.CATEGORY_WORD, sre.CATEGORY_NOT_WORD),
    ):
        for ascii_only in (False, True):
            subject = every[:128] if ascii_only else every
            runs = re.finditer(escape + "+", subject, re.ASCII if ascii_only else 0)
            matched = tuple((run.start(), run.end() - 1) for run in runs)
            found[category, ascii_only] = matched
            found[negated, ascii_only] = _invert(matched)
    return found


@functools.cache
def _case_classes() -> tuple[list[int], dict[int, tuple[int, ...]]]:
    """The code points ``re`` matches to others when ignoring case, sorted,
    and the others each is matched to.

    ``re`` matches two characters whose lowercase forms have the same
    uppercase, as the Kelvin sign and ``k`` do, or ``ſ`` and ``s``.
    """
    import _sre

    classes: dict[str, set[int]] = {}
    for code in filter(_sre.unicode_iscased, range(sys.maxunicode + 1)):
        lower = _sre.unicode_tolower(code)
        classes.setdefault(chr(lower).upper(), set()).update((code, lower))
    others = {
        code: tuple(members - {code})
        for members in classes.values()
        for code in members
    }
    return sorted(others), others


def _either_case(ranges: _Ranges) -> _Ranges:
    """*ranges*, and every character ``re`` matches to one in them when
    ignoring case."""
    cased, others = _case_classes()
    found = list(ranges)
    for low, high in ranges:
        start, end = bisect.bisect_left(cased, low), bisect.bisect_right(cased, high)
        found.extend(
            (other, other)
            for code in cased[start:end]
            for other in others[code]
            if not low <= other <= high
        )
    return _span(found)


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
