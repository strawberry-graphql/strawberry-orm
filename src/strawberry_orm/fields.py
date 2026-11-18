"""Field helpers for strawberry-orm."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any
from weakref import WeakValueDictionary


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


#: Each combination of scopes composed once, while some field still uses it.
_EVERY_SCOPE: WeakValueDictionary[tuple[Any, ...], Callable[..., Any]] = (
    WeakValueDictionary()
)


def every_scope(
    scopes: Sequence[Callable[..., Any]],
    *,
    call: Callable[[Callable[..., Any], Any, Any], Any] = call_scope,
) -> Callable[..., Any]:
    """One ``scope=`` narrowing a query by each of *scopes* in turn, run by *call*.

    The same *scopes* and *call* compose to the same one: a relation cache is
    marked with the scope it was loaded under, and that is checked by identity.
    """
    key = (*scopes, call)
    try:
        found = _EVERY_SCOPE.get(key)
    except TypeError:
        return _composed(tuple(scopes), call)
    if found is None:
        found = _EVERY_SCOPE[key] = _composed(tuple(scopes), call)
    return found


def _composed(
    scopes: tuple[Any, ...], call: Callable[[Callable[..., Any], Any, Any], Any]
) -> Callable[..., Any]:
    def scoped(query: Any, info: Any) -> Any:
        for scope in scopes:
            query = call(scope, query, info)
        return query

    return scoped


def check_scope_signature(fn: Callable[..., Any]) -> None:
    """A scope narrows a query; it never sees the parent row."""
    names = _positional_names(fn)
    if names and names[0] == "self":
        raise TypeError(
            f"{getattr(fn, '__name__', 'scope')}(self, ...) is not a scope: a scope "
            f"receives (query, info) and never sees the parent row. Use "
            f"orm.field.lazy for a resolver that needs self."
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
            f"orm.field.{kind} runs once per parent row. Use orm.field.eager for a "
            f"callable that narrows the query instead."
        )
