"""StrawberryORM: unified entry point that delegates to the active backend."""

from __future__ import annotations

import typing as _typing
from typing import Any, Callable, Literal, Optional

import strawberry
from strawberry.extensions import SchemaExtension

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
            "order": Optional[order_type],
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
            "order": Optional[order_type],
        }
    else:

        def resolver(self: Any, info: Any) -> Any:
            return backend.get_default_queryset(model)

        resolver.__annotations__ = {"info": info_type}

    return resolver


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
    ) -> None:
        self._backend = backend
        self._description = description
        self._deprecation_reason = deprecation_reason

    def __set_name__(self, owner: type, name: str) -> None:
        ann = getattr(owner, "__annotations__", {}).get(name)
        if ann is None:
            return

        el_type = _extract_list_element(ann)
        if el_type is None:
            return

        model = getattr(el_type, "__orm_model__", None)
        if model is None:
            return

        f_type = getattr(el_type, "__orm_filter__", None)
        o_type = getattr(el_type, "__orm_order__", None)

        resolver = _make_query_resolver(self._backend, model, f_type, o_type)
        field = strawberry.field(
            resolver=resolver,
            description=self._description,
            deprecation_reason=self._deprecation_reason,
        )
        field._orm_auto_field = True  # type: ignore[attr-defined]
        setattr(owner, name, field)


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

    def aggregate(self, model: type, **kwargs: Any) -> Any:
        return self._backend.aggregate(model, **kwargs)

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
            model = _infer_model(filters, order)
            resolver = _make_query_resolver(
                self._backend,
                model,
                filters,
                order,
            )
            return strawberry.field(
                resolver=resolver,
                description=description,
                deprecation_reason=deprecation_reason,
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

    def connection(self, **kwargs: Any) -> Any:
        return self._backend.connection(**kwargs)

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
    ) -> None:
        return self._backend.apply_ref_list(
            instance, field, refs, info, authorize=authorize
        )

    # -- Queryset overrides --------------------------------------------------

    def is_query_object(self, value: Any) -> bool:
        return self._backend.is_query_object(value)

    def get_default_queryset(self, model: type) -> Any:
        return self._backend.get_default_queryset(model)

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return self._backend.optimizer_extension(**kwargs)


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
