"""StrawberryORM: unified entry point that delegates to the active backend."""

from __future__ import annotations

from inspect import Parameter, isawaitable
from types import UnionType
import typing as _typing
from typing import Any, Callable, Literal, Optional

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

from strawberry_orm._async import AwaitableOrValue, await_maybe
from strawberry_orm.backends.protocol import Backend
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
    return None


def _extract_output_type(ann: Any) -> Any:
    return _extract_list_element(ann) or _extract_connection_node(ann)


def _resolve_orm_metadata(
    ann: Any,
    *,
    filters: Any | None = None,
    order: Any | None = None,
) -> tuple[type | None, Any | None, Any | None]:
    if filters is not None or order is not None:
        return _infer_model_from_types(filters, order), filters, order

    output_type = _extract_output_type(ann)
    if output_type is None:
        return None, None, None

    model = getattr(output_type, "__orm_model__", None)
    if model is None:
        return None, None, None

    return (
        model,
        getattr(output_type, "__orm_filter__", None),
        getattr(output_type, "__orm_order__", None),
    )


def _make_query_resolver(
    backend: Backend,
    model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Build a resolver function with the correct parameter signature so
    Strawberry exposes ``filter`` and/or ``order`` as GraphQL arguments."""

    info_type = strawberry.types.Info

    if filter_type and order_type:

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
            "filter": Optional[filter_type],
            "order": Optional[list[order_type]],
        }
    elif filter_type:

        def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            query = backend.get_default_queryset(model)
            if filter is not None:
                query = backend.apply_filters(query, filter, model)
            return query

        resolver.__annotations__ = {
            "info": info_type,
            "filter": Optional[filter_type],
        }
    elif order_type:

        def resolver(self: Any, info: Any, order: Any = None) -> Any:
            query = backend.get_default_queryset(model)
            if order is not None:
                query = backend.apply_ordering(query, order, model)
            return query

        resolver.__annotations__ = {
            "info": info_type,
            "order": Optional[list[order_type]],
        }
    else:

        def resolver(self: Any, info: Any) -> Any:
            return backend.get_default_queryset(model)

        resolver.__annotations__ = {"info": info_type}

    return resolver


class _AutoFilterOrderExtension(FieldExtension):
    def __init__(
        self,
        backend: Backend,
        *,
        filters: Any | None = None,
        order: Any | None = None,
    ) -> None:
        self._backend = backend
        self._filters = filters
        self._order = order
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
        self._model, inferred_filter, inferred_order = _resolve_orm_metadata(
            annotation,
            filters=self._filters,
            order=self._order,
        )
        if self._filters is None:
            self._filters = inferred_filter
        if self._order is None:
            self._order = inferred_order

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
                    type_annotation=StrawberryAnnotation(Optional[self._filters]),
                    default=None,
                )
            )

        if self._order is not None and "order" not in existing:
            field.arguments.append(
                StrawberryArgument(
                    python_name="order",
                    graphql_name=None,
                    type_annotation=StrawberryAnnotation(Optional[list[self._order]]),
                    default=None,
                )
            )

    def _resolver_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        forwarded = dict(kwargs)
        if not self._resolver_accepts_filter:
            forwarded.pop("filter", None)
        if not self._resolver_accepts_order:
            forwarded.pop("order", None)
        return forwarded

    def _apply(
        self,
        result: Any,
        info: Any,
        *,
        filter: Any = None,
        order: Any = None,
    ) -> Any:
        if self._model is not None and self._backend.is_query_object(result):
            if filter is not None:
                result = self._backend.apply_filters(result, filter, self._model)
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
        )
        if isawaitable(result):
            return self._resolve_awaitable_result(result)
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

        result = self._apply(
            result,
            info,
            filter=kwargs.get("filter"),
            order=kwargs.get("order"),
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

        model, f_type, o_type = _resolve_orm_metadata(
            ann,
            filters=self._filters,
            order=self._order,
        )
        if model is None:
            return

        resolver = _make_query_resolver(self._backend, model, f_type, o_type)
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

        model, filter_type, order_type = _resolve_orm_metadata(graphql_type)
        if model is None:
            return

        resolver = _make_query_resolver(self._backend, model, filter_type, order_type)
        field = relay.connection(
            graphql_type,
            resolver=resolver,
            name=self._kwargs.get("name"),
            description=self._kwargs.get("description"),
            deprecation_reason=self._kwargs.get("deprecation_reason"),
            extensions=self._kwargs.get("extensions") or (),
            max_results=self._kwargs.get("max_results"),
        )
        field._orm_auto_field = True  # type: ignore[attr-defined]
        setattr(owner, name, field)

    def __call__(self, resolver: Callable[..., Any]) -> Any:
        extensions = list(self._kwargs.get("extensions") or [])
        extensions.append(_AutoFilterOrderExtension(self._backend))
        return relay.connection(
            self._graphql_type,
            resolver=resolver,
            name=self._kwargs.get("name"),
            description=self._kwargs.get("description"),
            deprecation_reason=self._kwargs.get("deprecation_reason"),
            extensions=extensions,
            max_results=self._kwargs.get("max_results"),
        )


class StrawberryORM:
    """Main entry point for strawberry-orm.

    Usage::

        orm = StrawberryORM("django")
        orm = StrawberryORM("sqlalchemy", dialect="postgresql", session_getter=get_session)
        orm = StrawberryORM("tortoise")
    """

    def __init__(self, backend: BackendName, **kwargs: Any) -> None:
        self._backend_name = backend
        self._backend: Backend = _create_backend(backend, **kwargs)

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

    # -- Fields --------------------------------------------------------------

    def field(
        self,
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
        delete: bool = False,
    ) -> type:
        return self._backend.ref(model, create=create, update=update, delete=delete)

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
        mode: str = "replace",
    ) -> AwaitableOrValue[None]:
        return self._backend.apply_ref_list(
            instance, field, refs, info, authorize=authorize, mode=mode
        )

    # -- Queryset overrides --------------------------------------------------

    def is_query_object(self, value: Any) -> bool:
        return self._backend.is_query_object(value)

    def get_default_queryset(self, model: type) -> Any:
        return self._backend.get_default_queryset(model)

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return self._backend.optimizer_extension(**kwargs)


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
