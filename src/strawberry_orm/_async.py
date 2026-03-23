"""Helpers for Strawberry-style mixed sync/async execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from inspect import isawaitable
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


def run_sync(
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


async def await_maybe(value: AwaitableOrValue[T]) -> T:
    """Await *value* when needed, otherwise return it unchanged."""
    if isawaitable(value):
        return await value
    return value
