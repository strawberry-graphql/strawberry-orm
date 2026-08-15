"""Field helpers for strawberry-orm."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def _positional_names(fn: Callable[..., Any]) -> list[str]:
    """Positional parameter names of *fn*, ignoring ``*args`` / keyword-only."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # pragma: no cover - builtins, C callables
        return []
    return [
        p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]


def call_scope(scope: Callable[..., Any], query: Any, info: Any) -> Any:
    """Invoke a ``scope=`` callable."""
    return scope(query, info)


def check_scope_signature(fn: Callable[..., Any]) -> None:
    """A scope narrows a query; it never sees the parent row."""
    names = _positional_names(fn)
    if names and names[0] == "self":
        raise TypeError(
            f"{getattr(fn, '__name__', 'scope')}(self, ...) is not a scope: a scope "
            f"receives (query, info) and never sees the parent row. Use "
            f"orm.field.custom for a resolver that needs self."
        )
    if len(names) != 2:
        raise TypeError(
            f"{getattr(fn, '__name__', 'scope')} takes {len(names)} positional "
            f"argument(s); a scope receives exactly (query, info)."
        )


def check_resolver_signature(fn: Callable[..., Any], kind: str) -> None:
    """A resolver runs per parent row, so it starts with ``self``."""
    names = _positional_names(fn)
    if not names or names[0] != "self":
        raise TypeError(
            f"{getattr(fn, '__name__', kind)} must take self as its first argument; "
            f"orm.field.{kind} runs once per parent row. Use orm.field.scoped for a "
            f"callable that narrows the query instead."
        )
