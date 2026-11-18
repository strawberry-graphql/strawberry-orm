"""The limits on a filter, counted across the whole of it.

A GraphQL variable can be referenced any number of times in one request, so
a limit on each list or each set of branches does not bound a filter: one
list within its limit can be repeated until the database binds millions of
values. Every branch and every listed value is counted here, each reference
to a variable included, before any SQL is built.

A ``regex`` or ``iRegex`` lookup costs up to time quadratic in a value's
length per row, and a text lookup (``contains``, ``startsWith``,
``endsWith`` and their ``i`` forms) time in its text's length times the
value's, so those are counted as well: on their own, across the filter, and
across every filter of a request, where aliases and relations can each hold
a filter within the limits.

A reference to a variable is two tokens however long its value, so the
size of every value is counted too, once for each place it is written, or
one long string referenced from every slot of a list under every alias is
bound to the database hundreds of times.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import strawberry

#: How many ``regex`` and ``iRegex`` lookups a request's filters hold, by default.
MAX_REGEX_FILTERS = 3
#: How many characters a request's text lookups hold in all, by default.
MAX_FILTER_TEXT_LENGTH = 1000
#: How many characters the values of a request's filters hold in all, by default.
MAX_FILTER_VALUE_LENGTH = 100_000

_BRANCH_KEYS = frozenset({"all", "any", "one_of"})
_REGEX_LOOKUPS = frozenset({"regex", "i_regex"})
_TEXT_LOOKUPS = frozenset(
    {
        "contains",
        "i_contains",
        "starts_with",
        "i_starts_with",
        "ends_with",
        "i_ends_with",
    }
)


def check_filter_budget(
    filter_input: Any,
    *,
    max_depth: int,
    max_branches: int,
    max_values: int,
    max_regex_filters: int = MAX_REGEX_FILTERS,
    max_text_length: int = MAX_FILTER_TEXT_LENGTH,
    max_value_length: int = MAX_FILTER_VALUE_LENGTH,
    info: Any = None,
) -> None:
    """Refuse *filter_input* when, taken whole, it is over a limit.

    ``max_branches`` counts the entries of every ``all``, ``any`` and
    ``oneOf`` list, and ``max_values`` every ``inList`` and ``notInList``
    value and the two ends of every ``range``. ``max_regex_filters`` counts
    every ``regex`` and ``iRegex`` lookup, ``max_text_length`` the
    characters of every text lookup, and ``max_value_length`` the characters
    of every value, a custom filter field's included, in this filter and,
    given the *info* of the field it filters, in all the request's filters:
    a field resolved once per parent is counted once.
    """
    limits = (max_regex_filters, max_text_length, max_value_length)
    branches = 0
    values = 0
    regexes = 0
    text = 0
    size = 0
    pending = [(filter_input, 0)]
    while pending:
        node, depth = pending.pop()
        if node is None or node is strawberry.UNSET:
            continue
        if depth > max_depth:
            raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")
        for key in node.__class__.__dataclass_fields__:
            value = getattr(node, key)
            if value is strawberry.UNSET or value is None:
                continue
            if key in _BRANCH_KEYS:
                branches += len(value)
                if branches > max_branches:
                    raise ValueError(
                        f"Filter has over {max_branches} branches in total; "
                        f"maximum is {max_branches}"
                    )
                pending.extend((branch, depth + 1) for branch in value)
            elif key == "not_":
                pending.append((value, depth + 1))
            elif key == "object":
                pending.extend(
                    (getattr(value, relation), depth + 1)
                    for relation in value.__class__.__dataclass_fields__
                )
            elif key == "field":
                listed, matched, written, measured = _field_costs(value)
                values += listed
                regexes += matched
                text += written
                size += measured
                if values > max_values:
                    raise ValueError(
                        f"Filter lists over {max_values} values in total; "
                        f"maximum is {max_values}"
                    )
            else:
                size += _size(value)
            _check_costs((regexes, text, size), limits, "Filter has")
    _charge_request(info, (regexes, text, size), limits)


_COSTS = (
    "regex lookups",
    "characters of text lookups",
    "characters of values",
)


def _check_costs(costs: tuple[int, ...], limits: tuple[int, ...], whose: str) -> None:
    for cost, limit, what in zip(costs, limits, _COSTS, strict=True):
        if cost > limit:
            raise ValueError(
                f"{whose} over {limit} {what} in total; maximum is {limit}"
            )


def _charge_request(info: Any, costs: tuple[int, ...], limits: tuple[int, ...]) -> None:
    """Add a filter's costs to its request's, refusing it when they go over.

    A field resolved once for each of many parents filters each with the same
    filter, so its costs are kept by field (its path, without list indices,
    and its nodes in the document) and counted once.
    """
    if info is None or not any(costs):
        return
    from strawberry_orm.batching import _cache_key, path_key
    from strawberry_orm.lazy_resolution import current_execution

    execution = current_execution()
    if execution is None:
        return
    charged = execution.filter_costs
    key = _cache_key(path_key(info), info)
    before = charged.get(key, (0,) * len(costs))
    after = tuple(map(max, before, costs))
    totals = [sum(column) for column in zip(*charged.values(), strict=True)] or [
        0
    ] * len(costs)
    _check_costs(
        tuple(
            total - was + now
            for total, was, now in zip(totals, before, after, strict=True)
        ),
        limits,
        "The request's filters have",
    )
    charged[key] = after


def _field_costs(field_input: Any) -> tuple[int, int, int, int]:
    """The values listed, regex lookups, characters of text lookups and of values."""
    listed = matched = written = measured = 0
    for column in field_input.__class__.__dataclass_fields__:
        lookup = getattr(field_input, column)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        measured += _size(lookup)
        for op_name in lookup.__class__.__dataclass_fields__:
            value = getattr(lookup, op_name)
            if value is strawberry.UNSET or value is None:
                continue
            if isinstance(value, list):
                listed += len(value)
            elif op_name == "range":
                listed += 2
            elif op_name in _REGEX_LOOKUPS:
                matched += 1
            elif op_name in _TEXT_LOOKUPS and isinstance(value, str):
                written += len(value)
    return listed, matched, written, measured


def _size(value: Any) -> int:
    """About how many characters binding *value* takes, each part counted.

    Walked rather than recursed: a JSON value can nest deeper than Python's
    stack, and none of it is bounded by GraphQL.
    """
    size = 0
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or item is strawberry.UNSET:
            continue
        if isinstance(item, (bool, float)):
            size += 1
        elif isinstance(item, (str, bytes, bytearray)):
            size += len(item)
        elif isinstance(item, int):
            # str() of a long int is itself refused past 4,300 digits.
            size += item.bit_length() // 3 + 1
        elif isinstance(item, Decimal):
            size += len(item.as_tuple().digits) + 1
        elif isinstance(item, dict):
            size += 1
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            size += 1
            pending.extend(item)
        elif hasattr(type(item), "__dataclass_fields__"):
            size += 1
            pending.extend(
                getattr(item, name) for name in type(item).__dataclass_fields__
            )
        else:
            size += len(str(item))
    return size
