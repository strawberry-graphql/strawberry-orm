"""Running an operation on a Strawberry schema from an MCP tool call."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    OperationDefinitionNode,
    parse,
    value_from_ast_untyped,
)
from mcp.server.mcpserver.exceptions import ToolError
from strawberry import Schema
from strawberry.extensions import SchemaExtension
from strawberry.schema.schema import validate_document
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

_SYNC_MUTATION_STOPPED = (
    "This mutation needs sync execution: SynchronousOnlyOperation stopped "
    "{stopped}{after}, and a mutation is not run a second time. Pass sync=True "
    "to mcp_server."
)


class Executor:
    """Runs operations with ``await schema.execute``, ``execute_sync`` in a
    worker thread, or a caller's own ``execute``.

    With ``sync=None``, a query that Django refuses on the event loop is
    rerun with ``execute_sync``, which is then kept for every later call. A
    mutation is not rerun, since whatever it did before its first ORM call
    would happen twice; its result comes back with an error naming the root
    field Django stopped and those that ran before it.

    ``prepare_off_loop=True`` has ``schema.execute`` parse and validate the
    operation in a worker thread, for one a client wrote; its resolvers
    still run on the event loop.
    """

    def __init__(
        self, schema: Any, *, sync: bool | None, execute: Execute | None
    ) -> None:
        if execute is not None and sync is not None:
            raise ValueError("Pass execute= or sync=, not both.")
        self.schema = schema
        self._off_loop = _preparing_off_loop(schema)
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
        *,
        prepare_off_loop: bool = False,
    ) -> Any:
        if self._execute is not None:
            return await self._execute(query, variables, context, set(allowed))
        if self.sync_execute:
            return await self._execute_sync(query, variables, context, allowed)
        schema = self._off_loop if prepare_off_loop else self.schema
        try:
            result = await schema.execute(
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
            if allowed != {OperationType.QUERY}:
                return _stopped(result, query, variables)
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


def _preparing_off_loop(schema: Any) -> Any:
    """A copy of *schema* with :class:`_PrepareOffLoop` after its own
    extensions, or *schema* itself if it is not a Strawberry schema.

    The copy shares everything else with *schema*, which is left as it is.
    """
    if not isinstance(schema, Schema):
        return schema
    view = copy.copy(schema)
    view.extensions = (*schema.extensions, _PrepareOffLoop)
    return view


class _PrepareOffLoop(SchemaExtension):
    """Parses and validates in a worker thread what ``schema.execute`` would
    parse and validate on the event loop.

    Coming after the schema's own extensions, it parses with the options and
    validates with the rules they set, and leaves an operation one of them
    already parsed or validated to them, as Strawberry does.
    """

    async def on_parse(self) -> AsyncIterator[None]:
        context = self.execution_context
        if context.graphql_document is None:
            # Strawberry parses a document refused here again, to report the
            # error the way it reports any other.
            with contextlib.suppress(GraphQLError):
                context.graphql_document = await asyncio.to_thread(
                    parse, context.query, **context.parse_options
                )
        yield

    async def on_validate(self) -> AsyncIterator[None]:
        context = self.execution_context
        if context.validation_rules and context.pre_execution_errors is None:
            context.pre_execution_errors = await asyncio.to_thread(
                validate_document,
                context.schema._schema,
                context.graphql_document,
                context.validation_rules,
            )
        yield


def _is_sync_only_operation(exc: BaseException) -> bool:
    """True when *exc* is Django refusing sync ORM work on the event loop."""
    try:
        from django.core.exceptions import SynchronousOnlyOperation
    except ImportError:
        return False
    return isinstance(exc, SynchronousOnlyOperation)


def _result_has_sync_only_operation(result: Any) -> bool:
    """True when Strawberry wrapped a ``SynchronousOnlyOperation`` in *result*."""
    return _sync_only_error(result) is not None


def _sync_only_error(result: Any) -> GraphQLError | None:
    """The first error in *result* that wraps a ``SynchronousOnlyOperation``."""
    for error in getattr(result, "errors", None) or ():
        original = getattr(error, "original_error", None)
        if original is not None and _is_sync_only_operation(original):
            return error
    return None


def _stopped(result: Any, query: str, variables: dict[str, Any]) -> Any:
    """*result* of a mutation ``SynchronousOnlyOperation`` stopped, with an
    error naming the root field it stopped and those that ran before it.

    A mutation's root fields run one after another, so those before it ran
    whether or not the data shows them: a non-null field's error leaves
    ``data`` empty.
    """
    error = _sync_only_error(result)
    assert error is not None
    stopped = error.path[0] if error.path else None
    keys = _root_fields(query, variables)
    ran = keys[: keys.index(stopped)] if stopped in keys else []
    after = f", after {', '.join(ran)} had run" if ran else ""
    hint = _SYNC_MUTATION_STOPPED.format(stopped=stopped or "it", after=after)
    result.errors = [*result.errors, GraphQLError(hint)]
    return result


def _root_fields(query: str, variables: dict[str, Any]) -> list[str]:
    """The response keys of the root fields of *query*'s operation, in the
    order they run, less those ``@skip`` or ``@include`` leaves out."""
    document = parse(query)
    [operation] = [
        d for d in document.definitions if isinstance(d, OperationDefinitionNode)
    ]
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    values = {
        d.variable.name.value: value_from_ast_untyped(d.default_value)
        for d in operation.variable_definitions or ()
        if d.default_value is not None
    }
    values.update(variables)
    keys: list[str] = []
    spread: set[str] = set()
    pending = list(reversed(operation.selection_set.selections))
    while pending:
        selection = pending.pop()
        if not _included(selection, values):
            continue
        if isinstance(selection, FieldNode):
            key = (selection.alias or selection.name).value
            if key not in keys:
                keys.append(key)
        elif isinstance(selection, InlineFragmentNode):
            pending.extend(reversed(selection.selection_set.selections))
        elif selection.name.value not in spread:
            spread.add(selection.name.value)
            fragment = fragments[selection.name.value]
            pending.extend(reversed(fragment.selection_set.selections))
    return keys


def _included(
    node: FieldNode | InlineFragmentNode | FragmentSpreadNode, values: dict[str, Any]
) -> bool:
    for directive in node.directives or ():
        name = directive.name.value
        if name in ("skip", "include") and value_from_ast_untyped(
            directive.arguments[0].value, values
        ) is (name == "skip"):
            return False
    return True


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
