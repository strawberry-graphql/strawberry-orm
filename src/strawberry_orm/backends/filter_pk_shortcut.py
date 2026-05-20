"""Object-traversal filters that only use :class:`~strawberry_orm.filters.ReferenceLookup`.

When ``object.<relation>.field.<pk>`` only applies :class:`ReferenceLookup` on the
related model's primary key, the filter can target the parent's forward FK column
(e.g. ``author_id``) without joining the related table.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import strawberry

from strawberry_orm.filters import is_fk_shortcut_lookup

_KNOWN_FILTER_COMBINATOR_KEYS = frozenset({"all", "any", "not_", "one_of"})


def filter_tree_uses_only_reference_lookups(
    filter_input: Any,
    *,
    custom_filter_keys: frozenset[str] = frozenset(),
) -> bool:
    """Return True when *filter_input* only applies ReferenceLookup on ``field``."""
    if filter_input is None or filter_input is strawberry.UNSET:
        return True

    fields = filter_input.__class__.__dataclass_fields__

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key in custom_filter_keys or key in ("object", "is_null"):
            return False

        if key == "field":
            if not _field_input_only_reference_lookups(val):
                return False
        elif key == "all" or key in ("any", "one_of"):
            if not all(
                filter_tree_uses_only_reference_lookups(
                    branch,
                    custom_filter_keys=custom_filter_keys,
                )
                for branch in val
            ):
                return False
        elif key == "not_":
            if not filter_tree_uses_only_reference_lookups(
                val,
                custom_filter_keys=custom_filter_keys,
            ):
                return False
        else:
            return False

    return True


def _field_input_only_reference_lookups(field_input: Any) -> bool:
    fields = field_input.__class__.__dataclass_fields__
    has_fk_lookup = False
    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        if not is_fk_shortcut_lookup(lookup):
            return False
        has_fk_lookup = True
    return has_fk_lookup


def build_reference_object_filter_clause(
    filter_input: Any,
    *,
    build_field_clause: Callable[..., Any],
    custom_filter_keys: frozenset[str] = frozenset(),
    max_branches: int = 50,
    **build_field_kwargs: Any,
) -> Any | None:
    """Build a clause for ``object.<relation>`` when only ReferenceLookup is used."""
    if not filter_tree_uses_only_reference_lookups(
        filter_input,
        custom_filter_keys=custom_filter_keys,
    ):
        return None

    return _build_reference_clause_recursive(
        filter_input,
        build_field_clause=build_field_clause,
        custom_filter_keys=custom_filter_keys,
        max_branches=max_branches,
        **build_field_kwargs,
    )


def _build_reference_clause_recursive(
    filter_input: Any,
    *,
    build_field_clause: Callable[..., Any],
    custom_filter_keys: frozenset[str],
    max_branches: int,
    **build_field_kwargs: Any,
) -> Any | None:
    if filter_input is None or filter_input is strawberry.UNSET:
        return None

    fields = filter_input.__class__.__dataclass_fields__

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            return build_field_clause(val, **build_field_kwargs)
        if key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = None
            for branch in val:
                sub = _build_reference_clause_recursive(
                    branch,
                    build_field_clause=build_field_clause,
                    custom_filter_keys=custom_filter_keys,
                    max_branches=max_branches,
                    **build_field_kwargs,
                )
                if sub is None:
                    return None
                combined = sub if combined is None else _combine_and(combined, sub)
            return combined
        if key in ("any", "one_of"):
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            combined = None
            for branch in val:
                sub = _build_reference_clause_recursive(
                    branch,
                    build_field_clause=build_field_clause,
                    custom_filter_keys=custom_filter_keys,
                    max_branches=max_branches,
                    **build_field_kwargs,
                )
                if sub is None:
                    return None
                combined = sub if combined is None else _combine_or(combined, sub)
            return combined
        if key == "not_":
            sub = _build_reference_clause_recursive(
                val,
                build_field_clause=build_field_clause,
                custom_filter_keys=custom_filter_keys,
                max_branches=max_branches,
                **build_field_kwargs,
            )
            if sub is None:
                return None
            return _combine_not(sub)

    return None


def _combine_and(left: Any, right: Any) -> Any:
    return left & right


def _combine_or(left: Any, right: Any) -> Any:
    return left | right


def _combine_not(value: Any) -> Any:
    return ~value
