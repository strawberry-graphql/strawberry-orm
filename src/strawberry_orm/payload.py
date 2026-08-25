"""Resolvers that report failure through a ``data`` / ``errors`` payload.

A GraphQL error aborts the field and gives the client an ``errors`` array
detached from the shape it asked for. Many APIs would rather answer with a
typed payload: ``data`` when the work succeeded, ``errors`` when it did not, so
a client can render the failure next to the thing that failed.

The convention is easy to write by hand and tedious to write forty times. This
builds the payload type from the resolver's own return annotation, turns
exceptions into your errors type, and keeps sync ORM work off the event loop.

Nothing here optimizes queries. ``data`` is a resolved field like any other, so
rows returned through it reach the optimizer with exactly the selection that
describes them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

import strawberry

from strawberry_orm._async import in_async_context, keep_annotations, run_sync


@dataclass(frozen=True)
class PayloadPolicy:
    """How resolvers turn a failure into an ``errors`` value.

    ``errors`` is the GraphQL type of the ``errors`` field. ``on_error``
    converts a caught exception into one; re-raise from it for anything you
    would rather surface as a GraphQL error.

    ``handles`` narrows what is caught at all. The default catches ``Exception``
    and leaves the decision to ``on_error``; naming your own exception types
    instead means an unexpected error keeps its traceback and its status.

    ``types`` is where ``errors`` and a resolver's return annotation are looked
    up when they name a type as a string. Naming the errors type is often the
    only option, since the module holding it usually imports the ORM itself. A schema that keeps its types in one module can point
    here once instead of importing every name into every module that resolves
    one.
    """

    errors: Any
    on_error: Callable[[BaseException], Any]
    handles: tuple[type[BaseException], ...] = (Exception,)
    suffix: str = "Payload"
    types: str | None = None


@dataclass
class _PayloadField:
    """One field on a generated payload type."""

    annotation: Any
    default: Any = None
    value: Any = dataclass_field(default=None)


def _resolve_annotation(annotation: Any, policy: PayloadPolicy) -> Any:
    """Turn names in *annotation* into the classes they refer to.

    Strawberry resolves a forward reference against the module that wrote it,
    which fails when a resolver names a type its own module never imported.
    ``PayloadPolicy.types`` says where to look instead, and the reference is
    replaced before the payload type is built.
    """
    if policy.types is None:
        return annotation

    import importlib
    import typing

    module = importlib.import_module(policy.types)

    def resolve(value: Any) -> Any:
        name = None
        if isinstance(value, str):
            name = value
        elif hasattr(value, "__forward_arg__"):
            name = value.__forward_arg__
        if name is not None:
            return getattr(module, name.rsplit(".", 1)[-1], value)

        args = typing.get_args(value)
        if not args:
            return value
        origin = typing.get_origin(value)
        resolved = tuple(resolve(arg) for arg in args)
        if resolved == args:
            return value
        try:
            return origin[resolved if len(resolved) > 1 else resolved[0]]
        except TypeError:  # pragma: no cover - exotic generics stay as written
            return value

    return resolve(annotation)


def _element_type(annotation: Any) -> Any:
    """The item type of a list annotation, or the annotation itself."""
    import typing

    if annotation is None:
        return None
    args = typing.get_args(annotation)
    if not args:
        return annotation
    # ``list[X]`` and ``X | None`` both hand back their first real argument.
    return next((arg for arg in args if arg is not type(None)), None)


def _payload_name(fn: Callable[..., Any], policy: PayloadPolicy, given: str | None):
    if given is not None:
        return given
    parts = re.split(r"_+", fn.__name__.strip("_"))
    return "".join(part[:1].upper() + part[1:] for part in parts if part) + (
        policy.suffix
    )


def _build_payload_type(
    name: str,
    module: str,
    fields: dict[str, _PayloadField],
) -> type:
    """Create the ``@strawberry.type`` holding ``data`` and ``errors``.

    ``__module__`` is the resolver's own so a string annotation such as
    ``list["PostType"]`` resolves against the module that wrote it.
    """
    namespace: dict[str, Any] = {
        "__annotations__": {
            key: spec.annotation for key, spec in fields.items() if spec.value is None
        },
        "__module__": module,
        "__doc__": f"Payload for {name}.",
    }
    for key, spec in fields.items():
        namespace[key] = spec.value if spec.value is not None else spec.default
    return strawberry.type(type(name, (), namespace), name=name)


def _guarded(
    fn: Callable[..., Any],
    policy: PayloadPolicy,
    build: Callable[[Any, Any], Any],
    failed: Callable[[BaseException], Any],
) -> Callable[..., Any]:
    """Run *fn*, off the event loop when there is one, and catch failures.

    The wrapper stays sync so ``execute_sync`` keeps working; under async it
    returns a coroutine. Deciding per call rather than per deployment means the
    same resolver is correct in tests and under an ASGI server.

    ``info`` is taken from the call rather than declared, so a resolver that
    does not want it never sees it.
    """

    @wraps(fn)
    def resolver(*args: Any, **kwargs: Any) -> Any:
        info = kwargs.get("info") or next(
            (arg for arg in args if hasattr(arg, "context")), None
        )
        if iscoroutinefunction(fn):

            async def _awaited() -> Any:
                try:
                    return build(await fn(*args, **kwargs), info)
                except policy.handles as exc:
                    return failed(exc)

            return _awaited()

        def call() -> Any:
            return fn(*args, **kwargs)

        if not in_async_context():
            try:
                return build(call(), info)
            except policy.handles as exc:
                return failed(exc)

        async def _offloaded() -> Any:
            try:
                return build(await run_sync(call, thread_sensitive=True), info)
            except policy.handles as exc:
                return failed(exc)

        return _offloaded()

    return keep_annotations(resolver, fn)


class PayloadFactory:
    """``orm.payload`` - resolvers that answer with ``data`` and ``errors``."""

    def __init__(self, orm: Any, policy: PayloadPolicy | None) -> None:
        self._orm = orm
        self._policy = policy

    def _errors_type(self, policy: PayloadPolicy) -> Any:
        """The errors type, resolved if it was named rather than imported."""
        return _resolve_annotation(policy.errors, policy)

    def _require_policy(self) -> PayloadPolicy:
        if self._policy is None:
            raise TypeError(
                "orm.payload needs a PayloadPolicy. Pass one when building the "
                "ORM: StrawberryORM.for_django(payload=PayloadPolicy(errors=..., "
                "on_error=...))."
            )
        return self._policy

    def query(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        permission_classes: list[type] | None = None,
    ) -> Any:
        """A query field whose result is wrapped in a payload."""
        return self._simple(fn, name=name, permissions=permission_classes)

    def mutation(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        permission_classes: list[type] | None = None,
        input_mutation: bool = False,
    ) -> Any:
        """A mutation whose result is wrapped in a payload.

        ``input_mutation=True`` collapses the arguments into a single generated
        ``input`` argument, via Strawberry's ``InputMutationExtension``.
        """
        return self._simple(
            fn,
            name=name,
            permissions=permission_classes,
            mutation=True,
            input_mutation=input_mutation,
        )

    def connection(
        self,
        graphql_type: Any = None,
        *,
        name: str | None = None,
        permission_classes: list[type] | None = None,
    ) -> Any:
        """A payload whose ``data`` is a Relay connection.

        The resolver returns rows; the generated ``filter`` / ``order`` /
        ``groupBy`` arguments and pagination hang off ``data`` as usual. On
        failure ``data`` is an empty connection rather than null, so a client
        can render the error without special-casing the shape.
        """
        policy = self._require_policy()

        def _decorate(fn: Callable[..., Any]) -> Any:
            from strawberry_orm.core import _extract_connection_node

            connection_type = graphql_type
            if connection_type is None:
                # Read the node type off the resolver, the same place
                # ``orm.payload.query`` reads its data type from.
                returns = _resolve_annotation(fn.__annotations__.get("return"), policy)
                node_type = _element_type(returns)
                if node_type is None:
                    raise TypeError(
                        f"{fn.__name__} needs a return annotation naming the "
                        f"node type, or an explicit connection type: "
                        f"orm.payload.connection(ORMListConnection[NodeType])."
                    )
                from strawberry_orm.relay import ORMListConnection

                connection_type = ORMListConnection[node_type]

            node = _extract_connection_node(connection_type)
            payload_name = _payload_name(fn, policy, name)

            def rows(self: Any, info: Any) -> Any:
                return self._orm_connection_rows

            rows.__annotations__ = {
                "info": strawberry.types.Info,
                "return": Sequence[node] if node is not None else Any,
            }

            payload = _build_payload_type(
                payload_name,
                fn.__module__,
                {
                    "errors": _PayloadField(self._errors_type(policy) | None),
                    "_orm_connection_rows": _PayloadField(strawberry.Private[Any]),
                    "data": _PayloadField(
                        None,
                        value=self._orm.connection(connection_type, resolver=rows),
                    ),
                },
            )

            fn.__annotations__ = {**fn.__annotations__, "return": payload}
            resolver = _guarded(
                fn,
                policy,
                lambda data, _info: payload(_orm_connection_rows=data, errors=None),
                lambda exc: payload(
                    _orm_connection_rows=[], errors=policy.on_error(exc)
                ),
            )
            return strawberry.field(
                resolver=resolver, permission_classes=permission_classes
            )

        return _decorate

    def _simple(
        self,
        fn: Callable[..., Any] | None,
        *,
        name: str | None,
        permissions: list[type] | None,
        mutation: bool = False,
        input_mutation: bool = False,
    ) -> Any:
        policy = self._require_policy()

        def _decorate(func: Callable[..., Any]) -> Any:
            data_type = func.__annotations__.get("return")
            if data_type is None:
                raise TypeError(
                    f"{func.__name__} needs a return annotation; orm.payload "
                    f"builds the payload type from what the resolver returns."
                )
            data_type = _resolve_annotation(data_type, policy)

            payload_name = _payload_name(func, policy, name)
            payload = _build_payload_type(
                payload_name,
                func.__module__,
                {
                    "data": _PayloadField(data_type | None),
                    "errors": _PayloadField(self._errors_type(policy) | None),
                },
            )

            # Strawberry reads the field type off the resolver, following
            # ``__wrapped__`` to get there, so the payload has to be declared
            # on the function being wrapped rather than on the wrapper.
            func.__annotations__ = {**func.__annotations__, "return": payload}
            resolver = _guarded(
                func,
                policy,
                lambda data, info: payload(
                    data=self._orm.optimize(data, info, at="data"), errors=None
                ),
                lambda exc: payload(data=None, errors=policy.on_error(exc)),
            )

            extensions = []
            if input_mutation:
                from strawberry.field_extensions import InputMutationExtension

                extensions.append(InputMutationExtension())

            build = strawberry.mutation if mutation else strawberry.field
            return build(
                resolver=resolver,
                permission_classes=permissions,
                extensions=extensions,
            )

        return _decorate(fn) if fn is not None else _decorate
