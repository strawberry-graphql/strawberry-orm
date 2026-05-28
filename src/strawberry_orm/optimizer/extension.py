"""Unified OptimizerExtension that delegates to the active backend."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

from strawberry_orm._async import await_maybe

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend
    from strawberry_orm.optimizer.store import OptimizerStore


def extensions_optimizer_index(extensions: list[Any]) -> int | None:
    """Return the index of the optimizer extension in *extensions*, or None."""
    for index, ext in enumerate(extensions):
        if isinstance(ext, type) and issubclass(ext, OptimizerExtension):
            return index
        if getattr(ext, "__name__", "").startswith("OptimizerExtension_"):
            return index
    return None


def extensions_include_optimizer(extensions: list[Any]) -> bool:
    """Return True if *extensions* already contains an optimizer extension."""
    return extensions_optimizer_index(extensions) is not None


class OptimizerExtension(SchemaExtension):
    """A Strawberry schema extension that reads the GraphQL query tree
    and tells the active backend to apply eager-loading / column-selection
    hints before the query is executed.

    The extension intercepts resolver return values. When a resolver returns
    a raw query object (SA ``Select``, Django ``QuerySet``), the optimizer
    walks the GraphQL selection set (including inline fragments and spreads),
    adds eager-loads, and executes the query
    so that downstream resolvers receive model instances instead.
    """

    _backend: Backend | None = None
    _store: OptimizerStore | None = None

    @classmethod
    def configure(
        cls, backend: Backend, store: OptimizerStore
    ) -> type[OptimizerExtension]:
        """Return a configured subclass bound to a specific backend/store."""
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_store": store},
        )

    def on_execute(self) -> Any:
        yield

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = _next(root, info, *args, **kwargs)
        backend = self._backend
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            result = backend.apply_optimizer_hints(self._store, result, info)

        if isawaitable(result):
            return self._resolve_async(result, info)

        return result

    async def _resolve_async(self, result: Any, info: Any) -> Any:
        result = await await_maybe(result)

        backend = self._backend
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            result = await await_maybe(
                backend.apply_optimizer_hints(self._store, result, info)
            )

        return result
