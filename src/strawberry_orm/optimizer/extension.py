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


def _extensions_from_info(info: Any) -> list[Any]:
    schema = getattr(info, "schema", None)
    if schema is None:
        return []
    strawberry_schema = getattr(schema, "_strawberry_schema", schema)
    return list(getattr(strawberry_schema, "extensions", None) or [])


def schema_includes_optimizer(info: Any) -> bool:
    """Return True when the executing schema has an optimizer extension."""
    return extensions_include_optimizer(_extensions_from_info(info))


def get_configured_optimizer(info: Any) -> tuple[Backend | None, OptimizerStore | None]:
    """Return the backend and store bound to the schema's optimizer extension."""
    extensions = _extensions_from_info(info)
    index = extensions_optimizer_index(extensions)
    if index is None:
        return None, None
    extension = extensions[index]
    return (
        getattr(extension, "_backend", None),
        getattr(extension, "_store", None),
    )


def optimize_query_nodes(nodes: Any, info: Any) -> Any:
    """Apply optimizer hints to *nodes* when it is still a backend query object."""
    backend, store = get_configured_optimizer(info)
    if backend is None or store is None or not backend.is_query_object(nodes):
        return nodes
    return backend.apply_optimizer_hints(store, nodes, info)


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
