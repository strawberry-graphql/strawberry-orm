"""Running an operation on a Strawberry schema from an MCP tool call."""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from strawberry.types.graphql import OperationType

Execute = Callable[[str, dict[str, Any], Any, set[OperationType]], Awaitable[Any]]

logger = logging.getLogger(__name__)

_SYNC_EXECUTE_WARNING = (
    "The MCP server fell back to schema.execute_sync in a worker thread because "
    "await schema.execute raised SynchronousOnlyOperation. Sync resolvers "
    "(for example Django's ORM) cannot run on the event loop; later calls "
    "keep using execute_sync. Pass sync=True to skip the first, failed attempt."
)

_SYNC_MUTATION_ERROR = (
    "This mutation needs sync execution: await schema.execute raised "
    "SynchronousOnlyOperation, and a mutation is not run a second time. "
    "Pass sync=True to mcp_server."
)


class Executor:
    """Runs operations with ``await schema.execute``, ``execute_sync`` in a
    worker thread, or a caller's own ``execute``.

    With ``sync=None``, a query that Django refuses on the event loop is
    rerun with ``execute_sync``, which is then kept for every later call. A
    mutation is not rerun, since whatever it did before its first ORM call
    would happen twice.
    """

    def __init__(
        self, schema: Any, *, sync: bool | None, execute: Execute | None
    ) -> None:
        if execute is not None and sync is not None:
            raise ValueError("Pass execute= or sync=, not both.")
        self.schema = schema
        self._execute = execute
        self._sync = sync
        #: ``True`` once execution runs ``execute_sync`` in a worker thread.
        self.sync_execute = sync is True

    async def run(
        self,
        query: str,
        variables: dict[str, Any],
        context: Any,
        allowed: set[OperationType],
    ) -> Any:
        if self._execute is not None:
            return await self._execute(query, variables, context, set(allowed))
        if self.sync_execute:
            return await self._execute_sync(query, variables, context, allowed)
        try:
            result = await self.schema.execute(
                query,
                variable_values=variables,
                context_value=context,
                allowed_operation_types=allowed,
            )
        except Exception as exc:
            if self._sync is False or not _is_sync_only_operation(exc):
                raise
            return await self._switch(query, variables, context, allowed)
        if self._sync is None and _result_has_sync_only_operation(result):
            return await self._switch(query, variables, context, allowed)
        return result

    async def _switch(
        self,
        query: str,
        variables: dict[str, Any],
        context: Any,
        allowed: set[OperationType],
    ) -> Any:
        if allowed != {OperationType.QUERY}:
            raise ToolError(_SYNC_MUTATION_ERROR)
        self.sync_execute = True
        logger.warning(_SYNC_EXECUTE_WARNING)
        warnings.warn(_SYNC_EXECUTE_WARNING, UserWarning, stacklevel=3)
        return await self._execute_sync(query, variables, context, allowed)

    async def _execute_sync(
        self,
        query: str,
        variables: dict[str, Any],
        context: Any,
        allowed: set[OperationType],
    ) -> Any:
        def run() -> Any:
            close = _django_close_old_connections()
            close()
            try:
                return self.schema.execute_sync(
                    query,
                    variable_values=variables,
                    context_value=context,
                    allowed_operation_types=allowed,
                )
            finally:
                close()

        return await asyncio.to_thread(run)


def _is_sync_only_operation(exc: BaseException) -> bool:
    """True when *exc* is Django refusing sync ORM work on the event loop."""
    try:
        from django.core.exceptions import SynchronousOnlyOperation
    except ImportError:
        return False
    return isinstance(exc, SynchronousOnlyOperation)


def _result_has_sync_only_operation(result: Any) -> bool:
    """True when Strawberry wrapped a ``SynchronousOnlyOperation`` in *result*."""
    for error in getattr(result, "errors", None) or ():
        original = getattr(error, "original_error", None)
        if original is not None and _is_sync_only_operation(original):
            return True
    return False


def _django_close_old_connections() -> Callable[[], None]:
    """Django's ``close_old_connections``, or a no-op without a configured Django.

    Django's handler calls it on every request start and finish; an executor
    thread gets neither, so without it the thread keeps its connection past
    ``CONN_MAX_AGE`` and never notices one the database dropped.
    """
    try:
        from django.conf import settings
        from django.db import close_old_connections
    except ImportError:
        return _no_op
    return close_old_connections if settings.configured else _no_op


def _no_op() -> None:
    return None
