"""The limits on a filter, counted across the whole of it.

A GraphQL variable can be referenced any number of times in one request, so
a limit on each list or each set of branches does not bound a filter: one
list within its limit can be repeated until the database binds millions of
values. Every branch and every listed value is counted here, each reference
to a variable included, before any SQL is built.
"""

from __future__ import annotations

from typing import Any

import strawberry

_BRANCH_KEYS = frozenset({"all", "any", "one_of"})


def check_filter_budget(
    filter_input: Any,
    *,
    max_depth: int,
    max_branches: int,
    max_values: int,
) -> None:
    """Refuse *filter_input* when, taken whole, it is over a limit.

    ``max_branches`` counts the entries of every ``all``, ``any`` and
    ``oneOf`` list, and ``max_values`` every ``inList`` and ``notInList``
    value and the two ends of every ``range``.
    """
    branches = 0
    values = 0
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
                values += _listed_values(value)
                if values > max_values:
                    raise ValueError(
                        f"Filter lists over {max_values} values in total; "
                        f"maximum is {max_values}"
                    )


def _listed_values(field_input: Any) -> int:
    count = 0
    for column in field_input.__class__.__dataclass_fields__:
        lookup = getattr(field_input, column)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        for op_name in lookup.__class__.__dataclass_fields__:
            value = getattr(lookup, op_name)
            if isinstance(value, list):
                count += len(value)
            elif op_name == "range" and value not in (None, strawberry.UNSET):
                count += 2
    return count
