"""Object-traversal filters that only use :class:`~strawberry_orm.filters.ReferenceLookup`.

When ``object.<relation>.field.<pk>`` only applies :class:`ReferenceLookup` on the
related model's primary key, the filter can target the parent's forward FK column
(e.g. ``author_id``) without joining the related table. The FK holds that key
and nothing else, so any other column of the related model is compared on the
related row.
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
    key: str,
    custom_filter_keys: frozenset[str] = frozenset(),
) -> bool:
    """Return True when *filter_input* only applies ReferenceLookup on ``field.<key>``."""
    if filter_input is None or filter_input is strawberry.UNSET:
        return True

    fields = filter_input.__class__.__dataclass_fields__

    for key_name in fields:
        val = getattr(filter_input, key_name)
        if val is strawberry.UNSET or val is None:
            continue

        if key_name in custom_filter_keys or key_name in ("object", "is_null"):
            return False

        if key_name == "field":
            if not _field_input_only_reference_lookups(val, key):
                return False
        elif key_name == "all" or key_name in ("any", "one_of"):
            if not all(
                filter_tree_uses_only_reference_lookups(
                    branch,
                    key=key,
                    custom_filter_keys=custom_filter_keys,
                )
                for branch in val
            ):
                return False
        elif key_name == "not_":
            if not filter_tree_uses_only_reference_lookups(
                val,
                key=key,
                custom_filter_keys=custom_filter_keys,
            ):
                return False
        else:
            return False

    return True


def _field_input_only_reference_lookups(field_input: Any, key: str) -> bool:
    fields = field_input.__class__.__dataclass_fields__
    has_fk_lookup = False
    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        if col_name != key or not is_fk_shortcut_lookup(lookup):
            return False
        has_fk_lookup = True
    return has_fk_lookup


def build_reference_object_filter_clause(
    filter_input: Any,
    *,
    key: str,
    build_field_clause: Callable[..., Any],
    custom_filter_keys: frozenset[str] = frozenset(),
    max_branches: int = 50,
    max_depth: int = 10,
    depth: int = 0,
    **build_field_kwargs: Any,
) -> Any | None:
    """Build a clause for ``object.<relation>`` when only ReferenceLookup is used.

    *key* is the related model's field the parent's foreign key holds, and
    *depth* the nesting level *filter_input* sits at in the whole filter.
    """
    if not filter_tree_uses_only_reference_lookups(
        filter_input,
        key=key,
        custom_filter_keys=custom_filter_keys,
    ):
        return None

    return _build_reference_clause_recursive(
        filter_input,
        build_field_clause=build_field_clause,
        custom_filter_keys=custom_filter_keys,
        max_branches=max_branches,
        max_depth=max_depth,
        _depth=depth,
        **build_field_kwargs,
    )


def _build_reference_clause_recursive(
    filter_input: Any,
    *,
    build_field_clause: Callable[..., Any],
    custom_filter_keys: frozenset[str],
    max_branches: int,
    max_depth: int = 10,
    _depth: int = 0,
    **build_field_kwargs: Any,
) -> Any | None:
    if _depth > max_depth:
        raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")

    if filter_input is None or filter_input is strawberry.UNSET:
        return None

    fields = filter_input.__class__.__dataclass_fields__
    recurse_kw = dict(
        build_field_clause=build_field_clause,
        custom_filter_keys=custom_filter_keys,
        max_branches=max_branches,
        max_depth=max_depth,
        _depth=_depth + 1,
        **build_field_kwargs,
    )

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
                sub = _build_reference_clause_recursive(branch, **recurse_kw)
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
                sub = _build_reference_clause_recursive(branch, **recurse_kw)
                if sub is None:
                    return None
                combined = sub if combined is None else _combine_or(combined, sub)
            return combined
        if key == "not_":
            sub = _build_reference_clause_recursive(val, **recurse_kw)
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
