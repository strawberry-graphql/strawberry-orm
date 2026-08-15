"""Unified OptimizerExtension that delegates to the active backend."""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

from strawberry_orm._async import await_maybe
from strawberry_orm.batching import stash_parents

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


def returns_single_orm_object(backend: Backend, info: Any) -> bool:
    """Return True when the field yields one ORM object rather than a list.

    Backends materialize query objects into lists, so a field annotated
    ``PostType | None`` needs the single row lifted back out. Connections and
    other wrapper types are left alone: their named type is not a registered
    ORM type.
    """
    from graphql import GraphQLList, GraphQLNonNull

    return_type = getattr(info, "return_type", None)
    while isinstance(return_type, GraphQLNonNull):
        return_type = return_type.of_type
    if return_type is None or isinstance(return_type, GraphQLList):
        return False

    name = getattr(return_type, "name", None)
    registry = getattr(backend, "_type_registry", {})
    return name is not None and name in registry


def _unwrap_single(result: Any, singular: bool) -> Any:
    if not singular or not isinstance(result, list):
        return result
    return result[0] if result else None


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
        singular = False
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            result = backend.apply_optimizer_hints(self._store, result, info)

        if isawaitable(result):
            return self._resolve_async(result, info, singular)

        stash_parents(getattr(self, "execution_context", None), info, result)
        return _unwrap_single(result, singular)

    async def _resolve_async(
        self, result: Any, info: Any, singular: bool = False
    ) -> Any:
        result = await await_maybe(result)

        backend = self._backend
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            result = await await_maybe(
                backend.apply_optimizer_hints(self._store, result, info)
            )

        stash_parents(getattr(self, "execution_context", None), info, result)
        return _unwrap_single(result, singular)
