"""StrawberryORM: unified entry point that delegates to the active backend."""

from __future__ import annotations

import sys
import typing as _typing
from collections.abc import Callable
from functools import partial, wraps
from inspect import Parameter, isawaitable, iscoroutinefunction
from types import UnionType
from typing import Any, Literal

import strawberry
from strawberry import relay
from strawberry.annotation import StrawberryAnnotation
from strawberry.extensions import SchemaExtension
from strawberry.extensions.field_extension import (
    AsyncExtensionResolver,
    FieldExtension,
    SyncExtensionResolver,
)
from strawberry.types.arguments import StrawberryArgument
from strawberry.types.cast import cast as strawberry_cast

from strawberry_orm._async import (
    AwaitableOrValue,
    await_maybe,
    await_maybe_blocking,
    in_async_context,
    materialize_result,
    run_orm_work,
    run_orm_work_blocking,
)
from strawberry_orm.backends.protocol import Backend
from strawberry_orm.mutations import MutationNamespace
from strawberry_orm.types import FieldDefinition

BackendName = Literal["django", "sqlalchemy", "tortoise"]


def _extract_list_element(ann: Any) -> Any:
    """Return *T* from ``list[T]``, or ``None``."""
    if _typing.get_origin(ann) is list:
        args = _typing.get_args(ann)
        if args:
            return args[0]
    return None


def _unwrap_optional_annotation(ann: Any) -> Any:
    origin = _typing.get_origin(ann)
    if origin not in (_typing.Union, UnionType):
        return ann

    args = [arg for arg in _typing.get_args(ann) if arg is not type(None)]
    if len(args) == 1:
        return _unwrap_optional_annotation(args[0])
    return ann


def _extract_connection_node(ann: Any) -> Any:
    ann = _unwrap_optional_annotation(ann)
    origin = _typing.get_origin(ann)
    if origin and isinstance(origin, type) and issubclass(origin, relay.Connection):
        args = _typing.get_args(ann)
        if args:
            return args[0]
    if isinstance(ann, type) and hasattr(ann, "_node_type"):
        return ann._node_type
    return None


def _extract_output_type(ann: Any) -> Any:
    return _extract_list_element(ann) or _extract_connection_node(ann)


def _resolve_orm_metadata(
    ann: Any,
    *,
    filters: Any | None = None,
    order: Any | None = None,
) -> tuple[type | None, Any | None, Any | None, Any | None, Any | None]:
    """Return ``(model, filter_type, order_type, group_type, aggregate_type)``."""
    if filters is not None or order is not None:
        return _infer_model_from_types(filters, order), filters, order, None, None

    output_type = _extract_output_type(ann)
    if output_type is None:
        return None, None, None, None, None

    model = getattr(output_type, "__orm_model__", None)
    if model is None:
        return None, None, None, None, None

    return (
        model,
        getattr(output_type, "__orm_filter__", None),
        getattr(output_type, "__orm_order__", None),
        getattr(output_type, "__orm_group__", None),
        getattr(output_type, "__orm_aggregate__", None),
    )


def _make_query_resolver(
    backend: Backend,
    model: type,
    filter_type: Any,
    order_type: Any,
    group_type: Any = None,
) -> Any:
    """Build a resolver function with the correct parameter signature so
    Strawberry exposes ``filter``, ``order``, and optionally ``groupBy``
    as GraphQL arguments."""

    info_type = strawberry.types.Info

    if group_type:

        def resolver(
            self: Any,
            info: Any,
            filter: Any = None,
            order: Any = None,
            group_by: Any = None,
        ) -> Any:
            query = backend.get_default_queryset(model)
            if filter is not None:
                query = backend.apply_filters(query, filter, model)
            if order is not None:
                query = backend.apply_ordering(query, order, model)
            return query

        annotations: dict[str, Any] = {"info": info_type}
        if filter_type:
            annotations["filter"] = filter_type | None
        if order_type:
            annotations["order"] = list[order_type] | None
        annotations["group_by"] = list[group_type] | None
        resolver.__annotations__ = annotations
    elif filter_type and order_type:

        def resolver(
            self: Any, info: Any, filter: Any = None, order: Any = None
        ) -> Any:
            query = backend.get_default_queryset(model)
            if filter is not None:
                query = backend.apply_filters(query, filter, model)
            if order is not None:
                query = backend.apply_ordering(query, order, model)
            return query

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
            "order": list[order_type] | None,
        }
    elif filter_type:

        def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            query = backend.get_default_queryset(model)
            if filter is not None:
                query = backend.apply_filters(query, filter, model)
            return query

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
        }
    elif order_type:

        def resolver(self: Any, info: Any, order: Any = None) -> Any:
            query = backend.get_default_queryset(model)
            if order is not None:
                query = backend.apply_ordering(query, order, model)
            return query

        resolver.__annotations__ = {
            "info": info_type,
            "order": list[order_type] | None,
        }
    else:

        def resolver(self: Any, info: Any) -> Any:
            return backend.get_default_queryset(model)

        resolver.__annotations__ = {"info": info_type}

    wrap = getattr(backend, "wrap_async_safe", None)
    if wrap is not None:
        resolver = wrap(resolver, materialize=False)
    return resolver


class _AutoFilterOrderExtension(FieldExtension):
    def __init__(
        self,
        backend: Backend,
        *,
        filters: Any | None = None,
        order: Any | None = None,
        group: Any | None = None,
    ) -> None:
        self._backend = backend
        self._filters = filters
        self._order = order
        self._group = group
        self._model: type | None = None
        self._output_type: Any | None = None
        self._resolver_accepts_filter = False
        self._resolver_accepts_order = False
        self._is_configured = False

    def _configure(self, field: Any) -> None:
        if self._is_configured:
            return

        annotation = getattr(field.type_annotation, "annotation", None)
        self._output_type = _extract_output_type(annotation)
        self._model, inferred_filter, inferred_order, inferred_group, _inferred_agg = (
            _resolve_orm_metadata(
                annotation,
                filters=self._filters,
                order=self._order,
            )
        )
        if self._filters is None:
            self._filters = inferred_filter
        if self._order is None:
            self._order = inferred_order
        if self._group is None:
            self._group = inferred_group

        params = ()
        if field.base_resolver is not None:
            params = field.base_resolver.signature.parameters.values()

        self._resolver_accepts_filter = any(
            p.name == "filter" or p.kind == Parameter.VAR_KEYWORD for p in params
        )
        self._resolver_accepts_order = any(
            p.name == "order" or p.kind == Parameter.VAR_KEYWORD for p in params
        )
        self._is_configured = True

    def apply(self, field: Any) -> None:
        self._configure(field)

        existing = {arg.python_name for arg in field.arguments}
        if self._filters is not None and "filter" not in existing:
            field.arguments.append(
                StrawberryArgument(
                    python_name="filter",
                    graphql_name=None,
                    type_annotation=StrawberryAnnotation(self._filters | None),
                    default=None,
                )
            )

        if self._order is not None and "order" not in existing:
            field.arguments.append(
                StrawberryArgument(
                    python_name="order",
                    graphql_name=None,
                    type_annotation=StrawberryAnnotation(list[self._order] | None),
                    default=None,
                )
            )

        if self._group is not None and "group_by" not in existing:
            field.arguments.append(
                StrawberryArgument(
                    python_name="group_by",
                    graphql_name="groupBy",
                    type_annotation=StrawberryAnnotation(list[self._group] | None),
                    default=None,
                )
            )

    def _resolver_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(kwargs)
        if not self._resolver_accepts_filter:
            forwarded.pop("filter", None)
        if not self._resolver_accepts_order:
            forwarded.pop("order", None)
        forwarded.pop("group_by", None)
        return forwarded

    def _stash_context(
        self,
        info: Any,
        base_query: Any,
        *,
        group_by: Any = None,
        order: Any = None,
    ) -> None:
        """Stash the filtered (pre-pagination) query and backend on context."""
        ctx = info.context
        if ctx is None:
            return
        if isinstance(ctx, dict):
            ctx["_orm_base_query"] = base_query
            ctx["_orm_backend"] = self._backend
            ctx["_orm_group_by"] = group_by
            ctx["_orm_order"] = order
        else:
            ctx._orm_base_query = base_query  # type: ignore[attr-defined]
            ctx._orm_backend = self._backend  # type: ignore[attr-defined]
            ctx._orm_group_by = group_by  # type: ignore[attr-defined]
            ctx._orm_order = order  # type: ignore[attr-defined]

    def _run_filter_work(
        self,
        result: Any,
        info: Any,
        *,
        filter: Any = None,
        order: Any = None,
        group_by: Any = None,
    ) -> Any:
        if self._model is not None and self._backend.is_query_object(result):
            if filter is not None:
                result = self._backend.apply_filters(result, filter, self._model)

            self._stash_context(info, result, group_by=group_by, order=order)

            if order is not None:
                result = self._backend.apply_ordering(result, order, self._model)
            result = materialize_result(
                self._backend,
                result,
                info,
                sync=True,
            )

        return result

    def _apply(
        self,
        result: Any,
        info: Any,
        *,
        filter: Any = None,
        order: Any = None,
        group_by: Any = None,
    ) -> Any:
        if self._model is not None and self._backend.is_query_object(result):
            if in_async_context():
                return run_orm_work_blocking(
                    self._run_filter_work,
                    result,
                    info,
                    filter=filter,
                    order=order,
                    group_by=group_by,
                )

            if filter is not None:
                result = self._backend.apply_filters(result, filter, self._model)

            self._stash_context(info, result, group_by=group_by, order=order)

            if order is not None:
                result = self._backend.apply_ordering(result, order, self._model)
            result = self._backend.materialize_query(result, info)

        return result

    def _cast_result(self, result: Any) -> Any:
        if self._output_type is None:
            return result
        if isinstance(result, (list, tuple)):
            return [strawberry_cast(self._output_type, item) for item in result]
        return result

    async def _resolve_awaitable_result(self, result: Any) -> Any:
        return self._cast_result(await await_maybe(result))

    def resolve(
        self,
        next_: SyncExtensionResolver,
        source: Any,
        info: Any,
        **kwargs: Any,
    ) -> Any:
        result = next_(source, info, **self._resolver_kwargs(kwargs))
        result = self._apply(
            result,
            info,
            filter=kwargs.get("filter"),
            order=kwargs.get("order"),
            group_by=kwargs.get("group_by"),
        )
        if isawaitable(result):
            result = await_maybe_blocking(result)
        return self._cast_result(result)

    async def resolve_async(
        self,
        next_: AsyncExtensionResolver,
        source: Any,
        info: Any,
        **kwargs: Any,
    ) -> Any:
        result = next_(source, info, **self._resolver_kwargs(kwargs))
        if isawaitable(result):
            result = await await_maybe(result)

        self._resolver_kwargs(kwargs)
        if self._model is not None and self._backend.is_query_object(result):
            materialize = self._backend.materialize_query
            if iscoroutinefunction(materialize):
                if kwargs.get("filter") is not None:
                    result = self._backend.apply_filters(
                        result,
                        kwargs["filter"],
                        self._model,
                    )
                self._stash_context(
                    info,
                    result,
                    group_by=kwargs.get("group_by"),
                    order=kwargs.get("order"),
                )
                if kwargs.get("order") is not None:
                    result = self._backend.apply_ordering(
                        result,
                        kwargs["order"],
                        self._model,
                    )
                result = await materialize(result, info)
                return self._cast_result(result)

            result = await await_maybe(
                run_orm_work(
                    partial(
                        self._run_filter_work,
                        result,
                        info,
                        filter=kwargs.get("filter"),
                        order=kwargs.get("order"),
                        group_by=kwargs.get("group_by"),
                    ),
                    thread_sensitive=True,
                )
            )
            return self._cast_result(result)

        result = self._apply(
            result,
            info,
            filter=kwargs.get("filter"),
            order=kwargs.get("order"),
            group_by=kwargs.get("group_by"),
        )
        if isawaitable(result):
            result = await await_maybe(result)
        return self._cast_result(result)


class _AutoField:
    """Descriptor returned by ``orm.field()`` that auto-detects ``filter``
    and ``order`` from the return-type's ``__orm_filter__`` /
    ``__orm_order__`` attributes at class-creation time via
    ``__set_name__``.

    This mirrors the strawberry-django pattern where filters declared on
    the *type* are automatically inherited by every field that returns
    that type.
    """

    _orm_auto_field = True

    def __init__(
        self,
        backend: Backend,
        description: str | None = None,
        deprecation_reason: str | None = None,
        *,
        filters: Any | None = None,
        order: Any | None = None,
    ) -> None:
        self._backend = backend
        self._description = description
        self._deprecation_reason = deprecation_reason
        self._filters = filters
        self._order = order

    def __set_name__(self, owner: type, name: str) -> None:
        ann = getattr(owner, "__annotations__", {}).get(name)
        if ann is None:
            return

        model, f_type, o_type, g_type, _a_type = _resolve_orm_metadata(
            ann,
            filters=self._filters,
            order=self._order,
        )
        if model is None:
            return

        resolver = _make_query_resolver(self._backend, model, f_type, o_type, g_type)
        field = strawberry.field(
            resolver=resolver,
            description=self._description,
            deprecation_reason=self._deprecation_reason,
        )
        field._orm_auto_field = True  # type: ignore[attr-defined]
        setattr(owner, name, field)

    def __call__(self, resolver: Callable[..., Any]) -> Any:
        return strawberry.field(
            resolver=resolver,
            description=self._description,
            deprecation_reason=self._deprecation_reason,
            extensions=[
                _AutoFilterOrderExtension(
                    self._backend,
                    filters=self._filters,
                    order=self._order,
                )
            ],
        )


class _AutoConnection:
    _orm_auto_field = True

    def __init__(
        self,
        backend: Backend,
        graphql_type: Any | None,
        **kwargs: Any,
    ) -> None:
        self._backend = backend
        self._graphql_type = graphql_type
        self._kwargs = kwargs

    def __set_name__(self, owner: type, name: str) -> None:
        graphql_type = self._graphql_type or getattr(owner, "__annotations__", {}).get(
            name
        )
        if graphql_type is None:
            return

        model, filter_type, order_type, group_type, aggregate_type = (
            _resolve_orm_metadata(graphql_type)
        )
        if model is None:
            return

        node_type = _extract_connection_node(graphql_type)

        if group_type is not None:
            graphql_type = _build_grouped_connection(
                self._backend,
                graphql_type,
                model,
                group_type,
                order_type,
                aggregate_type=aggregate_type,
            )

        base_resolver = _make_query_resolver(
            self._backend, model, filter_type, order_type, group_type
        )
        if iscoroutinefunction(getattr(self._backend, "materialize_query", None)):

            @wraps(base_resolver)
            async def resolver(*args: Any, **kwargs: Any) -> Any:
                return base_resolver(*args, **kwargs)

        else:
            resolver = base_resolver
        if node_type is not None:
            resolver.__annotations__["return"] = list[node_type]
        extensions = list(self._kwargs.get("extensions") or [])
        extensions.append(
            _AutoFilterOrderExtension(
                self._backend,
                filters=filter_type,
                order=order_type,
                group=group_type,
            )
        )
        field = relay.connection(
            graphql_type,
            resolver=resolver,
            name=self._kwargs.get("name"),
            description=self._kwargs.get("description"),
            deprecation_reason=self._kwargs.get("deprecation_reason"),
            extensions=extensions,
            max_results=self._kwargs.get("max_results"),
        )
        field._orm_auto_field = True  # type: ignore[attr-defined]
        field._orm_connection = True  # type: ignore[attr-defined]
        setattr(owner, name, field)

    def __call__(self, resolver: Callable[..., Any]) -> Any:
        extensions = list(self._kwargs.get("extensions") or [])
        extensions.append(_AutoFilterOrderExtension(self._backend))
        field = relay.connection(
            self._graphql_type,
            resolver=resolver,
            name=self._kwargs.get("name"),
            description=self._kwargs.get("description"),
            deprecation_reason=self._kwargs.get("deprecation_reason"),
            extensions=extensions,
            max_results=self._kwargs.get("max_results"),
        )
        field._orm_connection = True  # type: ignore[attr-defined]
        return field


def _build_grouped_connection(
    backend: Backend,
    connection_type: Any,
    model: type,
    group_type: Any,
    order_type: Any | None,
    *,
    aggregate_type: Any | None = None,
) -> Any:
    """Dynamically generate connection/page-info/group types with aggregation.

    Returns a new connection type that extends the original with
    ``aggregates``, ``groups``, and an extended ``PageInfo``.
    """
    import types as _types_mod

    from strawberry_orm.backends._base import AggregateMeta
    from strawberry_orm.relay.connection import ORMListConnection, PageInfo

    meta: AggregateMeta = backend._build_aggregate_types(model, aggregate_type)  # type: ignore[union-attr]
    model_name = model.__name__
    AggregatesType = meta.aggregates_type
    GroupKeyType = meta.group_key_type

    page_info_ns: dict[str, Any] = {
        "__annotations__": {
            "start_cursor": str | None,
            "end_cursor": str | None,
            "has_previous_page": bool,
            "has_next_page": bool,
            "aggregates": AggregatesType | None,
        },
        "aggregates": None,
    }
    ExtPageInfo = strawberry.type(
        type(f"{model_name}PageInfo", (PageInfo,), page_info_ns),
        name=f"{model_name}PageInfo",
    )

    node_type = _extract_connection_node(connection_type)

    # -- Group items connection (cursor-based pagination) --------------------

    GroupItemsConnection: Any = None
    if node_type is not None:
        from strawberry.relay import ListConnection as _ListConnection

        def _items_exec_body(ns: dict[str, Any]) -> None:
            pass

        GroupItemsConnection = _types_mod.new_class(
            f"{model_name}GroupItemsConnection",
            (_ListConnection[node_type],),  # type: ignore[misc]
            exec_body=_items_exec_body,
        )
        GroupItemsConnection = strawberry.type(
            GroupItemsConnection,
            name=f"{model_name}GroupItemsConnection",
        )

    # -- Group type ----------------------------------------------------------

    _items_conn_cls = GroupItemsConnection
    _items_node_type = node_type

    def _items_resolver(
        self: Any,
        info: strawberry.types.Info,
        first: int | None = None,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> Any:
        nodes = getattr(self, "_items_nodes", [])
        cast_nodes = [strawberry_cast(_items_node_type, n) for n in nodes]
        return _items_conn_cls.resolve_connection(
            cast_nodes,
            info=info,
            first=first,
            after=after,
            last=last,
            before=before,
        )

    group_ns: dict[str, Any] = {
        "__annotations__": {
            "key": GroupKeyType,
            "aggregates": AggregatesType,
            "edge_indices": list[int],
        },
        "edge_indices": strawberry.field(default_factory=list),
    }

    if GroupItemsConnection is not None:
        _items_resolver.__annotations__["return"] = GroupItemsConnection
        group_ns["items"] = strawberry.field(resolver=_items_resolver)
        group_ns["__annotations__"]["items"] = GroupItemsConnection

    GroupType = strawberry.type(
        type(f"{model_name}Group", (), group_ns),
        name=f"{model_name}Group",
    )

    conn_ns: dict[str, Any] = {
        "__annotations__": {
            "page_info": ExtPageInfo,
            "aggregates": AggregatesType | None,
            "groups": list[GroupType] | None,
        },
        "aggregates": None,
        "groups": None,
        "_orm_aggregate_meta": meta,
        "_page_info_type": ExtPageInfo,
    }

    if node_type is not None:

        def _exec_body(ns: dict[str, Any]) -> None:
            ns.update(conn_ns)

        NewConnection = _types_mod.new_class(
            f"{model_name}Connection",
            (ORMListConnection[node_type],),  # type: ignore[misc]
            exec_body=_exec_body,
        )
    else:
        NewConnection = type(f"{model_name}Connection", (ORMListConnection,), conn_ns)

    NewConnection = strawberry.type(
        NewConnection,
        name=f"{model_name}Connection",
    )
    NewConnection._orm_aggregate_meta = meta  # type: ignore[attr-defined]
    NewConnection._page_info_type = ExtPageInfo  # type: ignore[attr-defined]
    NewConnection._node_type = node_type  # type: ignore[attr-defined]

    return NewConnection


class StrawberryORM:
    """Main entry point for strawberry-orm.

    Usage::

        orm = StrawberryORM.for_django()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="postgresql",
            session_getter=get_session,
        )
        orm = StrawberryORM.for_tortoise()

    """

    def __init__(self) -> None:
        raise TypeError(
            "Use StrawberryORM.for_django(), StrawberryORM.for_sqlalchemy(), "
            "or StrawberryORM.for_tortoise() to create an instance."
        )

    def _configure(self, backend: BackendName, **kwargs: Any) -> None:
        from strawberry_orm.repo import AbstractRepo

        self._backend_name = backend
        repos: dict[type, type[AbstractRepo]] | None = kwargs.pop("repos", None)  # type: ignore[type-arg]
        policy = kwargs.pop("policy", None)
        self._backend: Backend = _create_backend(backend, **kwargs)
        self._backend._repos = repos or {}  # type: ignore[attr-defined]

        if policy is not None and not repos:
            from strawberry_orm.policy import _policy_to_repos

            self._backend._repos = _policy_to_repos(policy)  # type: ignore[attr-defined]

        self.mutations = MutationNamespace(self._backend)

    @classmethod
    def _construct(cls, backend: BackendName, **kwargs: Any) -> StrawberryORM:
        orm = object.__new__(cls)
        orm._configure(backend, **kwargs)
        return orm

    @classmethod
    def for_django(cls, **kwargs: Any) -> StrawberryORM:
        """Create an ORM configured for Django."""
        return cls._construct("django", **kwargs)

    @classmethod
    def for_sqlalchemy(
        cls,
        *,
        dialect: str = "postgresql",
        session_getter: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> StrawberryORM:
        """Create an ORM configured for SQLAlchemy."""
        return cls._construct(
            "sqlalchemy",
            dialect=dialect,
            session_getter=session_getter,
            **kwargs,
        )

    @classmethod
    def for_tortoise(cls, **kwargs: Any) -> StrawberryORM:
        """Create an ORM configured for Tortoise ORM."""
        return cls._construct("tortoise", **kwargs)

    @property
    def backend(self) -> Backend:
        return self._backend

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        return self._backend.type(model, **kwargs)

    def input(self, model: type, **kwargs: Any) -> Any:
        return self._backend.input(model, **kwargs)

    def partial(self, model: type, **kwargs: Any) -> Any:
        return self._backend.partial(model, **kwargs)

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        return self._backend.filter(model_or_type, **kwargs)

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        return self._backend.order(model_or_type, **kwargs)

    def filter_type(self, model: type, **kwargs: Any) -> Any:
        return self._backend.filter_type(model, **kwargs)

    def order_type(self, model: type, **kwargs: Any) -> Any:
        return self._backend.order_type(model, **kwargs)

    def group(self, model_or_type: type, **kwargs: Any) -> Any:
        return self._backend.group(model_or_type, **kwargs)

    def group_type(self, model: type, **kwargs: Any) -> Any:
        return self._backend.group_type(model, **kwargs)

    def aggregate(self, model_or_type: type, **kwargs: Any) -> Any:
        return self._backend.aggregate(model_or_type, **kwargs)

    def aggregate_type(self, model: type, **kwargs: Any) -> Any:
        return self._backend.aggregate_type(model, **kwargs)

    # -- Fields --------------------------------------------------------------

    def field(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        filters: Any | None = None,
        order: Any | None = None,
        load: list[Any] | Callable[..., Any] | None = None,
        only: list[str] | None = None,
        compute: dict[str, Any] | None = None,
        disable_optimization: bool = False,
        description: str | None = None,
        deprecation_reason: str | None = None,
    ) -> Any:
        if fn is not None:
            wrap = getattr(self._backend, "wrap_async_safe", None)
            resolver = wrap(fn) if wrap is not None else fn
            return strawberry.field(resolver=resolver)

        if filters is not None or order is not None:
            return _AutoField(
                self._backend,
                description=description,
                deprecation_reason=deprecation_reason,
                filters=filters,
                order=order,
            )

        has_hints = any([load, only, compute, disable_optimization])
        if has_hints:
            return FieldDefinition(
                load=load,
                only=only,
                compute=compute,
                disable_optimization=disable_optimization,
                description=description,
            )

        return _AutoField(
            self._backend,
            description=description,
            deprecation_reason=deprecation_reason,
        )

    def node(self, **kwargs: Any) -> Any:
        return self._backend.node(**kwargs)

    def connection(self, graphql_type: Any | None = None, **kwargs: Any) -> Any:
        return _AutoConnection(self._backend, graphql_type, **kwargs)

    # -- Mutations -----------------------------------------------------------

    def create(self, input_type: type, **kwargs: Any) -> Any:
        return self._backend.create(input_type, **kwargs)

    def update(self, input_type: type, **kwargs: Any) -> Any:
        return self._backend.update(input_type, **kwargs)

    def delete(self, **kwargs: Any) -> Any:
        return self._backend.delete(**kwargs)

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        return self._backend.apply_filters(query, filter_input, model)

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        return self._backend.apply_ordering(query, order_input, model)

    # -- Related list refs ---------------------------------------------------

    def ref(
        self,
        model: type,
        *,
        create: type | None = None,
        update: type | None = None,
        unlink: bool = False,
        delete: bool = False,
    ) -> type:
        return self._backend.ref(
            model, create=create, update=update, unlink=unlink, delete=delete
        )

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
    ) -> AwaitableOrValue[None]:
        return self._backend.apply_ref_list(
            instance,
            field,
            refs,
            info,
            authorize=authorize,
        )

    # -- Queryset overrides --------------------------------------------------

    def is_query_object(self, value: Any) -> bool:
        return self._backend.is_query_object(value)

    def get_default_queryset(self, model: type) -> Any:
        return self._backend.get_default_queryset(model)

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return self._backend.optimizer_extension(**kwargs)

    def schema(self, *, optimizer: bool | None = None, **kwargs: Any) -> Any:
        """Create a :class:`strawberry.Schema` with the optimizer enabled by default.

        Pass ``optimizer=False`` to opt out, or set ``enable_optimizer=False`` when
        constructing the ORM instance. Additional ``extensions`` are merged after
        the optimizer unless an optimizer extension was already provided.
        """
        from strawberry_orm.lazy_resolution import extensions_include_lazy_resolution
        from strawberry_orm.optimizer.extension import extensions_include_optimizer

        if optimizer is None:
            optimizer = self._backend._enable_optimizer

        extensions = list(kwargs.pop("extensions", None) or [])

        if optimizer and not extensions_include_optimizer(extensions):
            extensions.insert(0, self.optimizer_extension())

        lazy_mode = getattr(self._backend, "_lazy_resolution", "off")
        if lazy_mode != "off" and not extensions_include_lazy_resolution(extensions):
            insert_at = 1 if extensions_include_optimizer(extensions) else 0
            extensions.insert(insert_at, self.lazy_resolution_extension(mode=lazy_mode))

        return strawberry.Schema(extensions=extensions, **kwargs)

    def lazy_resolution_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        from strawberry_orm.lazy_resolution import LazyResolutionExtension

        mode = kwargs.pop("mode", None) or getattr(
            self._backend, "_lazy_resolution", "warn"
        )
        return LazyResolutionExtension.configure(self._backend, mode=mode, **kwargs)


def _infer_model_from_types(filters: Any | None, order: Any | None) -> type:
    """Extract the ORM model class from filter/order types via ``__orm_model__``."""
    for source in (filters, order):
        if source is not None:
            model = getattr(source, "__orm_model__", None)
            if model is not None:
                return model
    raise ValueError(
        "Cannot infer model: neither the filter nor the order type has "
        "an __orm_model__ attribute.  Pass types created by orm.filter() / orm.order()."
    )


def _create_backend(name: BackendName, **kwargs: Any) -> Backend:
    if "warn_missing_queryset" not in kwargs and "pytest" in sys.modules:
        kwargs["warn_missing_queryset"] = False

    if name == "django":
        from strawberry_orm.backends.django import DjangoBackend

        return DjangoBackend(**kwargs)
    elif name == "sqlalchemy":
        from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend

        return SQLAlchemyBackend(**kwargs)
    elif name == "tortoise":
        from strawberry_orm.backends.tortoise import TortoiseBackend

        return TortoiseBackend(**kwargs)
    else:
        raise ValueError(
            f"Unknown backend {name!r}. Choose from: django, sqlalchemy, tortoise"
        )
