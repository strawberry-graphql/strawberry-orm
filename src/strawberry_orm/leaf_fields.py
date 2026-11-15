"""Keep the library's schema extensions off fields that only read a scalar.

Strawberry runs every extension that implements ``resolve`` around every field,
so a page of rows pays for the optimizer, batcher and connection hooks once per
column per row. None of them can act on a field whose GraphQL type is a scalar
or enum and whose value is a column read straight off the row, so those fields
skip them. An app's own ``resolve`` extensions still run everywhere.

Anything else read off a row keeps the hooks: a property can load a relation,
which lazy resolution is there to catch.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from graphql import (
    GraphQLEnumType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
)
from graphql.execution.middleware import MiddlewareManager


def leaf_skipping_schema_class(base: type, backend: Any) -> type:
    """A subclass of *base* whose plain scalar fields skip the library's hooks."""
    return type(base.__name__, (_LeafSkippingSchema, base), {"_orm_backend": backend})


@cache
def _library_resolves() -> frozenset[Any]:
    from strawberry_orm.batching import BatchingExtension, RelationConnectionExtension
    from strawberry_orm.lazy_resolution import LazyResolutionExtension
    from strawberry_orm.optimizer.extension import OptimizerExtension, RowScopeExtension

    return frozenset(
        extension.resolve
        for extension in (
            OptimizerExtension,
            RowScopeExtension,
            BatchingExtension,
            RelationConnectionExtension,
            LazyResolutionExtension,
        )
    )


def _is_library_extension(extension: Any) -> bool:
    """Whether *extension*'s ``resolve`` is one of the library's own.

    A subclass that overrides ``resolve`` is the app's: what it adds may well
    act on a scalar.
    """
    return type(extension).resolve in _library_resolves()


def _function(func: Any) -> Any:
    return getattr(func, "__func__", func)


def is_default_relay_id(graphql_type: type, func: Any) -> bool:
    """Whether *func* is Relay's ``id`` resolver, reading an unmodified ``resolve_id``."""
    from strawberry.relay import Node

    if not isinstance(graphql_type, type) or not issubclass(graphql_type, Node):
        return False
    relay_id = next(
        field.base_resolver.wrapped_func
        for field in Node.__strawberry_definition__.fields  # type: ignore[attr-defined]
        if field.python_name == "_id"
    )
    return _function(func) is _function(relay_id) and _function(
        graphql_type.resolve_id
    ) is _function(Node.resolve_id)


def _built_by_the_library(origin: Any) -> bool:
    """Whether every object of this type is one the library or Relay built.

    Such an object is never a row, so nothing on it can load a relation. A
    generic type Strawberry specialises is a subclass of the original.
    """
    from strawberry import relay

    return isinstance(origin, type) and (
        origin.__module__.startswith("strawberry_orm.")
        or issubclass(origin, (relay.Edge, relay.PageInfo, relay.Connection))
    )


def _columns(backend: Any, model: type) -> frozenset[str]:
    try:
        fields = backend._introspect_model(model)
    except Exception:  # pragma: no cover - defensive; nothing is skipped
        return frozenset()
    return frozenset(
        name for name, _type, is_relation, _related in fields if not is_relation
    )


def _leaf_resolvers(graphql_schema: Any, backend: Any) -> frozenset[Any]:
    """Resolvers of fields that return a scalar or enum read off the row as is."""
    leaves = set()
    columns: dict[type, frozenset[str]] = {}
    for graphql_type in graphql_schema.type_map.values():
        if not isinstance(graphql_type, GraphQLObjectType):
            continue
        definition = (graphql_type.extensions or {}).get("strawberry-definition")
        origin = getattr(definition, "origin", None)
        model = getattr(origin, "__orm_model__", None)
        if model is not None:
            if model not in columns:
                columns[model] = _columns(backend, model)
            readable: frozenset[str] | None = columns[model]
        elif _built_by_the_library(origin):
            readable = None
        else:
            continue
        for graphql_field in graphql_type.fields.values():
            of_type = graphql_field.type
            while isinstance(of_type, (GraphQLNonNull, GraphQLList)):
                of_type = of_type.of_type
            if not isinstance(of_type, (GraphQLScalarType, GraphQLEnumType)):
                continue
            field = (graphql_field.extensions or {}).get("strawberry-definition")
            if field is None:
                continue
            read = _attribute_read(origin, field)
            if read is None or (readable is not None and read not in readable):
                continue
            leaves.add(graphql_field.resolve)
    return frozenset(leaves)


def _attribute_read(origin: Any, field: Any) -> str | None:
    """The attribute *field* returns as is, or ``None`` if it runs other code."""
    resolver = field.base_resolver
    if resolver is None:
        return field.python_name
    if getattr(field, "_orm_relay_id", False) or is_default_relay_id(
        origin, resolver.wrapped_func
    ):
        try:
            return origin.resolve_id_attr()
        except Exception:
            return None
    if getattr(field, "_orm_basic_resolver", False):
        return field.python_name
    return None


class _LeafSkippingManager(MiddlewareManager):
    def __init__(
        self,
        middlewares: list[Any],
        leaves: frozenset[Any],
        leaf_middlewares: list[Any],
    ) -> None:
        super().__init__(*middlewares)
        self._leaves = leaves
        self._leaf_manager = (
            MiddlewareManager(*leaf_middlewares) if leaf_middlewares else None
        )

    def get_field_resolver(self, field_resolver: Any) -> Any:
        cached = self._cached_resolvers.get(field_resolver)
        if cached is None:
            if field_resolver in self._leaves:
                cached = (
                    field_resolver
                    if self._leaf_manager is None
                    else self._leaf_manager.get_field_resolver(field_resolver)
                )
            else:
                cached = super().get_field_resolver(field_resolver)
            self._cached_resolvers[field_resolver] = cached
        return cached


class _LeafSkippingSchema:
    """Mixed in ahead of a ``strawberry.Schema`` by :func:`leaf_skipping_schema_class`."""

    _orm_backend: Any = None

    def _get_middleware_manager(self, extensions: list[Any]) -> MiddlewareManager:
        middlewares = [ext for ext in extensions if ext._implements_resolve()]
        leaves = self.__dict__.get("_orm_leaf_resolvers")
        if leaves is None:
            leaves = _leaf_resolvers(self._schema, self._orm_backend)  # type: ignore[attr-defined]
            self.__dict__["_orm_leaf_resolvers"] = leaves
        return _LeafSkippingManager(
            middlewares,
            leaves,
            [ext for ext in middlewares if not _is_library_extension(ext)],
        )
