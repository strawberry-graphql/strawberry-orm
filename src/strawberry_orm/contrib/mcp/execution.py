"""Running an operation on a Strawberry schema from an MCP tool call."""

from __future__ import annotations

import asyncio
import copy
import logging
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    validate,
    value_from_ast_untyped,
)
from graphql.pyutils import Path
from mcp.server.mcpserver.exceptions import ToolError
from strawberry import Schema
from strawberry.extensions import SchemaExtension
from strawberry.types.graphql import OperationType

from .checks import VALIDATION_RULES, Costs

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

_SPENT = (
    "The arguments hold over {limit} values, each counted wherever a list "
    "repeats it; the limit is {limit}."
)


class Target:
    """Where a tool's operations run: *schema*, executing them on
    *graphql_schema* (by default its own), with the document each call read
    in a worker thread and no more argument values than the call may hold.

    A schema that is not a Strawberry schema runs them as it is.
    """

    def __init__(
        self,
        schema: Any,
        graphql_schema: GraphQLSchema | None = None,
        *,
        hide_suggestions: bool = False,
    ) -> None:
        self.graphql_schema = (
            getattr(schema, "_schema", None)
            if graphql_schema is None
            else graphql_schema
        )
        self.hide_suggestions = hide_suggestions
        self.awaited = _view(schema, graphql_schema, _PreparedAwaited)
        self.sync = _view(schema, graphql_schema, _PreparedSync)
        if graphql_schema is not None:
            graphql_schema._strawberry_schema = self.awaited  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Call:
    """One operation to run, read and measured in a worker thread."""

    query: str
    document: DocumentNode
    kind: OperationType
    variables: dict[str, Any]
    target: Target
    #: What its fields' arguments cost, or ``None`` when its variables do not
    #: coerce, so that it stops before any field runs.
    costs: Costs | None = None

    @property
    def writes(self) -> bool:
        """Whether it may run a mutation's resolvers: a mutation, or a query
        that selects fields of an interface the Mutation type implements."""
        return self.kind is OperationType.MUTATION or (
            self.costs is not None and self.costs.writes
        )


class Executor:
    """Runs operations with ``await schema.execute``, ``execute_sync`` in a
    worker thread, or a caller's own ``execute``.

    With ``sync=None``, a query that Django refuses on the event loop is
    rerun with ``execute_sync``, which is then kept for every later call. An
    operation that may run a mutation is not rerun, since whatever it did
    before its first ORM call would happen twice; its result comes back with
    an error naming the root field Django stopped and those that ran before
    it.

    Strawberry reuses the document a :class:`Call` holds rather than parse
    and validate it again, and stops each field whose arguments would take
    the call past the values its :class:`~.checks.Costs` allow.
    """

    def __init__(self, *, sync: bool | None, execute: Execute | None) -> None:
        if execute is not None and sync is not None:
            raise ValueError("Pass execute= or sync=, not both.")
        self._execute = execute
        self._sync = sync
        #: ``True`` once execution runs ``execute_sync`` in a worker thread.
        self.sync_execute = sync is True

    async def run(self, call: Call, context: Any) -> Any:
        if self._execute is not None:
            return await self._execute(call.query, call.variables, context, {call.kind})
        if self.sync_execute:
            return await self._execute_sync(call, context)
        token = _RUNNING.set(_Running(call))
        try:
            result = await call.target.awaited.execute(
                call.query,
                variable_values=call.variables,
                context_value=context,
                allowed_operation_types={call.kind},
            )
        except Exception as exc:
            if self._sync is False or not _is_sync_only_operation(exc):
                raise
            return await self._switch(call, context)
        finally:
            _RUNNING.reset(token)
        if self._sync is None and _result_has_sync_only_operation(result):
            if call.writes:
                return _stopped(result, call)
            return await self._switch(call, context)
        return result

    async def _switch(self, call: Call, context: Any) -> Any:
        if call.writes:
            raise ToolError(_SYNC_MUTATION_ERROR)
        self.sync_execute = True
        logger.warning(_SYNC_EXECUTE_WARNING)
        warnings.warn(_SYNC_EXECUTE_WARNING, UserWarning, stacklevel=3)
        return await self._execute_sync(call, context)

    async def _execute_sync(self, call: Call, context: Any) -> Any:
        def run() -> Any:
            close = _django_close_old_connections()
            close()
            _RUNNING.set(_Running(call))
            try:
                return call.target.sync.execute_sync(
                    call.query,
                    variable_values=call.variables,
                    context_value=context,
                    allowed_operation_types={call.kind},
                )
            finally:
                close()

        return await asyncio.to_thread(run)


class _Running:
    """A :class:`Call` as one attempt runs it, with the argument values it
    has left."""

    def __init__(self, call: Call) -> None:
        self.call = call
        self.left = 0 if call.costs is None else call.costs.max_values

    def spend(self, field: GraphQLField, nodes: list[FieldNode], path: Path) -> None:
        costs = self.call.costs
        if costs is None:
            return
        self.left -= costs.of(field, nodes[0])
        if self.left < 0:
            raise GraphQLError(
                _SPENT.format(limit=costs.max_values), nodes, path=path.as_list()
            )


_RUNNING: ContextVar[_Running | None] = ContextVar("mcp_running", default=None)


class _Budgeted:
    """Stops a field whose arguments would take its call past the values it
    may hold, before GraphQL coerces them."""

    def build_resolve_info(
        self,
        field_def: GraphQLField,
        field_nodes: list[FieldNode],
        parent_type: GraphQLObjectType,
        path: Path,
    ) -> Any:
        running = _RUNNING.get()
        if running is not None:
            running.spend(field_def, field_nodes, path)
        return super().build_resolve_info(  # type: ignore[misc]
            field_def, field_nodes, parent_type, path
        )


def _view(schema: Any, graphql_schema: GraphQLSchema | None, extension: type) -> Any:
    """A copy of *schema* that executes on *graphql_schema*, with *extension*
    after its own extensions and its execution class :class:`_Budgeted`, or
    *schema* itself if it is not a Strawberry schema.

    The copy shares everything else with *schema*, which is left as it is.
    """
    if not isinstance(schema, Schema):
        return schema
    view = copy.copy(schema)
    view.extensions = (*schema.extensions, extension)
    base = schema.execution_context_class
    view.execution_context_class = type(base.__name__, (_Budgeted, base), {})
    if graphql_schema is not None:
        view._schema = graphql_schema
    return view


class _Prepared(SchemaExtension):
    """Hands Strawberry the document of the call running, and validates it
    only with the rules the schema's own extensions add.

    Coming after those extensions, it leaves an operation one of them parsed
    or validated to them, and lets Strawberry parse it again with parse
    options other than a token limit the document is within.
    """

    def on_parse(self) -> Iterator[None]:
        running = _RUNNING.get()
        context = self.execution_context
        if (
            running is not None
            and context.graphql_document is None
            and _fits(running.call.document, context.parse_options)
        ):
            context.graphql_document = running.call.document
        yield

    def _left(self) -> tuple[tuple[Any, ...], Target] | None:
        """The rules the document is still to be validated with, and where
        it runs, or ``None`` if an extension validated it."""
        running = _RUNNING.get()
        context = self.execution_context
        if running is None or context.pre_execution_errors is not None:
            return None
        rules = tuple(r for r in context.validation_rules if r not in VALIDATION_RULES)
        return rules, running.call.target

    def _validate(self, rules: tuple[Any, ...], target: Target) -> list[GraphQLError]:
        context = self.execution_context
        assert context.graphql_document is not None
        return validate(
            context.schema._schema,
            context.graphql_document,
            rules,
            hide_suggestions=target.hide_suggestions,
        )


class _PreparedAwaited(_Prepared):
    async def on_validate(self) -> AsyncIterator[None]:
        left = self._left()
        if left is not None:
            rules, target = left
            self.execution_context.pre_execution_errors = (
                await asyncio.to_thread(self._validate, rules, target) if rules else []
            )
        yield


class _PreparedSync(_Prepared):
    def on_validate(self) -> Iterator[None]:
        left = self._left()
        if left is not None:
            rules, target = left
            self.execution_context.pre_execution_errors = (
                self._validate(rules, target) if rules else []
            )
        yield


def _fits(document: DocumentNode, options: Mapping[str, Any]) -> bool:
    """Whether parsing again with *options* gives *document*: they set at
    most a token limit, and it is within it."""
    limit = options.get("max_tokens")
    return options.keys() <= {"max_tokens"} and (
        limit is None or document.token_count <= limit
    )


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


def _stopped(result: Any, call: Call) -> Any:
    """*result* of a mutation ``SynchronousOnlyOperation`` stopped, with an
    error naming the root field it stopped and those that ran before it.

    A mutation's root fields run one after another, so those before it ran
    whether or not the data shows them: a non-null field's error leaves
    ``data`` empty.
    """
    error = _sync_only_error(result)
    assert error is not None
    stopped = error.path[0] if error.path else None
    keys = _root_fields(call.document, call.variables)
    ran = keys[: keys.index(stopped)] if stopped in keys else []
    after = f", after {', '.join(ran)} had run" if ran else ""
    hint = _SYNC_MUTATION_STOPPED.format(stopped=stopped or "it", after=after)
    result.errors = [*result.errors, GraphQLError(hint)]
    return result


def _root_fields(document: DocumentNode, variables: dict[str, Any]) -> list[str]:
    """The response keys of the root fields of *document*'s operation, in the
    order they run, less those ``@skip`` or ``@include`` leaves out."""
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
