"""Helpers for Strawberry-style mixed sync/async execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from inspect import isawaitable, iscoroutinefunction
from typing import Any, TypeVar
from weakref import WeakSet

T = TypeVar("T")

AwaitableOrValue = T | Awaitable[T]


def keep_annotations(wrapper: Any, wrapped: Any) -> Any:
    """Give *wrapper* the annotations *wrapped* declares.

    From 3.14 ``functools.wraps`` carries over the lazy annotation function
    rather than the computed annotations, and assigning ``__annotations__``
    clears that function - so wrapping a resolver whose annotations were set
    explicitly leaves the wrapper with none at all. Strawberry reads a field's
    arguments and return type from there.
    """
    wrapper.__annotations__ = dict(getattr(wrapped, "__annotations__", {}))
    return wrapper


def resolver_info(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """The ``Info`` a resolver was called with, or ``None`` if it takes none."""
    from strawberry.types.info import Info

    return next(
        (arg for arg in (*kwargs.values(), *args) if isinstance(arg, Info)), None
    )


def converting_resolver(backend: Any, func: Callable[..., Any]) -> Callable[..., Any]:
    """Hand what *func* returns to ``backend.to_query`` before anything reads it."""
    if not backend.converts_queries:
        return func
    to_query = backend.to_query

    if iscoroutinefunction(func):

        @wraps(func)
        async def converted(*args: Any, **kwargs: Any) -> Any:
            return to_query(await func(*args, **kwargs), resolver_info(args, kwargs))

    else:

        @wraps(func)
        def converted(*args: Any, **kwargs: Any) -> Any:
            return to_query(func(*args, **kwargs), resolver_info(args, kwargs))

    return keep_annotations(converted, func)


def in_async_context() -> bool:
    """Return ``True`` when called from a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync[T](
    func: Callable[..., T],
    *args: Any,
    thread_sensitive: bool = False,
    **kwargs: Any,
) -> AwaitableOrValue[T]:
    """Run *func* inline in sync code or offload it in async code."""
    if not in_async_context():
        return func(*args, **kwargs)

    async def runner() -> T:
        call = partial(func, *args, **kwargs)

        try:
            from asgiref.sync import sync_to_async
        except ImportError:
            return await asyncio.to_thread(call)

        return await sync_to_async(call, thread_sensitive=thread_sensitive)()

    return runner()


def materialize_result(
    backend: Any,
    value: Any,
    info: Any,
    *,
    sync: bool = False,
) -> Any:
    """Evaluate query objects to a concrete list when needed."""
    if not backend.is_query_object(value):
        return value
    if sync or not in_async_context():
        return list(value)
    return backend.materialize_query(value, info)


def run_orm_work[T](
    func: Callable[..., T],
    *args: Any,
    thread_sensitive: bool = True,
    **kwargs: Any,
) -> AwaitableOrValue[T]:
    """Run sync ORM work inline or via ``sync_to_async`` in async context."""
    return run_sync(func, *args, thread_sensitive=thread_sensitive, **kwargs)


def run_orm_work_blocking[T](
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run sync ORM work in a worker thread and block until complete.

    Used when a sync ``FieldExtension.resolve`` must return a plain value
    (e.g. Relay connections under ``AsyncGraphQLView``).
    """
    if not in_async_context():
        return func(*args, **kwargs)

    call = partial(func, *args, **kwargs)
    # A raw thread starts with an empty context; the work belongs to the
    # execution that asked for it, as it does under sync_to_async.
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, call).result()


async def await_maybe[T](value: AwaitableOrValue[T]) -> T:
    """Await *value* when needed, otherwise return it unchanged."""
    if isawaitable(value):
        return await value
    return value


def await_maybe_blocking[T](value: AwaitableOrValue[T]) -> T:
    """Resolve *value* to a concrete result for sync ``FieldExtension.resolve``.

    When the event loop is already running, awaitables are finished in a worker
    thread with a fresh loop so parent sync extensions (e.g. Relay connections)
    never receive an unawaited coroutine.
    """
    if not isawaitable(value):
        return value

    def finish() -> T:
        return asyncio.run(await_maybe(value))

    if not in_async_context():
        return finish()
    return run_orm_work_blocking(finish)


#: Wrappers made below. Wrapping one again would hop to the worker thread
#: before its own ``in_memory`` check could keep the read on the event loop.
#: Held by identity: ``functools.wraps`` copies attributes onto other wrappers.
_ASYNC_SAFE: WeakSet[Callable[..., Any]] = WeakSet()


def async_safe_resolver(
    func: Callable[..., Any],
    *,
    materialize: bool = True,
    thread_sensitive: bool = True,
    in_memory: Callable[..., bool] | None = None,
) -> Callable[..., Any]:
    """Wrap a sync resolver so ORM access runs off the event loop in async GraphQL.

    The wrapper stays a **sync** function so ``execute_sync`` keeps working; in an
    async context it returns an awaitable produced by :func:`run_sync`.

    ``in_memory`` is asked with the resolver's own arguments whether everything
    it reads is already loaded. When it is, the resolver runs on the event loop:
    a hop to the worker thread costs far more than reading a loaded value.
    """
    if iscoroutinefunction(func) or func in _ASYNC_SAFE:
        return func

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        def run() -> Any:
            result = func(*args, **kwargs)
            if materialize:
                try:
                    from django.db.models import QuerySet
                except ImportError:
                    QuerySet = ()  # type: ignore[misc, assignment]

                if isinstance(result, QuerySet):
                    return list(result)
            return result

        if not in_async_context():
            # Materialization only exists to keep a lazy queryset from being
            # iterated on the event loop; under sync execution it would just
            # deny the optimizer and the batcher a query object to work with.
            return func(*args, **kwargs)
        if in_memory is not None and in_memory(*args, **kwargs):
            return run()
        return run_sync(run, thread_sensitive=thread_sensitive)

    _ASYNC_SAFE.add(wrapper)
    return wrapper
