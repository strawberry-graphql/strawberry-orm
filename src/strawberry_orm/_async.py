"""Helpers for Strawberry-style mixed sync/async execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from inspect import isawaitable, iscoroutinefunction
from typing import Any, TypeVar

T = TypeVar("T")

AwaitableOrValue = T | Awaitable[T]


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
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call).result()


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


def async_safe_resolver(
    func: Callable[..., Any],
    *,
    materialize: bool = True,
    thread_sensitive: bool = True,
) -> Callable[..., Any]:
    """Wrap a sync resolver so ORM access runs off the event loop in async GraphQL.

    The wrapper stays a **sync** function so ``execute_sync`` keeps working; in an
    async context it returns an awaitable produced by :func:`run_sync`.
    """
    if iscoroutinefunction(func):
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
            return run()
        return run_sync(run, thread_sensitive=thread_sensitive)

    return wrapper
