"""Serve a Strawberry schema's operations as MCP tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import warnings
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from graphql import GraphQLSchema, assert_valid_schema, get_variable_values
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict
from strawberry.types.graphql import OperationType

from strawberry_orm._quoting import quoted_list

from .checks import (
    Costs,
    Parsed,
    check_variables,
    escaping_types,
    exposed_schema,
    exposed_sdl,
    reaching,
    read_operation,
    refuses_introspection,
)
from .execution import Call, Execute, Executor, Target
from .operations import Operation, read_operations

ContextGetter = Callable[[Any], Any]

_RUN_TOOL = "graphql"
_SCHEMA_TOOL = "graphql_schema"

_UNAUTHENTICATED_WARNING = (
    "This MCP server's tools can change data, and it has no authentication: "
    "anyone who can reach it over HTTP can call them. Pass token_verifier= and "
    "auth= to mcp_server, or serve it behind authentication of your own."
)


@dataclass(frozen=True)
class GraphQLTool:
    """A ``graphql`` tool that runs an operation the client writes, and a
    ``graphql_schema`` tool that returns the schema it is written against.

    Only queries run unless ``allow_mutations``. ``root_fields`` names the
    root fields it may use, bare or qualified by their root type, as
    ``Query.posts`` or ``Mutation.publish``; with ``allow_mutations``, a name
    both root types have must be qualified. The schema shows only those
    fields and the types they reach, through any field that returns a root
    type too, and operations run on the schema as it shows it, so no field,
    interface or union produces a value of a type it leaves out (with
    ``mcp_server(execute=...)``, a field that returns an interface or union
    that might is refused). An
    operation must be a single operation of at most ``max_length``
    characters, ``max_tokens`` tokens and ``max_depth`` levels that passes
    validation, may not define a fragment it does not use, select one field
    more than 20 times in one place of its result, nor more than
    ``max_fields`` fields with each fragment counted wherever it is spread
    and each alias counted, and may not ask for ``__schema`` or ``__type``:
    ``graphql_schema`` describes what it may use. ``descriptions=False``
    leaves the schema's descriptions and deprecation reasons out of
    ``graphql_schema``.

    ``schema_tool=False`` leaves ``graphql_schema`` out. A schema that refuses
    introspection (Strawberry's ``DisableIntrospection``) keeps its SDL from
    clients, so ``mcp_server`` will not hand it out unless ``schema_tool`` says
    which: ``True`` gives every MCP client the SDL all the same. Without
    ``graphql_schema``, on a schema that refuses introspection, or with
    ``StrawberryConfig(disable_field_suggestions=True)``, a refused
    operation's errors suggest no names, and neither do those of variables
    that do not fit the types it declares.

    The length limit is checked before anything reads the operation, and
    the token limit as it is parsed. It is parsed, checked and validated
    once, in a worker thread, and ``schema.execute`` runs that document,
    validating it, in a worker thread too, only with the rules the schema's
    own extensions add. Those extensions run once it executes: an extension
    such as ``ParserCache`` or ``ValidationCache`` still parses or validates
    it again on the event loop, as Strawberry does for a ``MaxTokensLimiter``
    the operation is past, to report it.
    """

    allow_mutations: bool = False
    root_fields: Collection[str] | None = None
    max_depth: int = 10
    descriptions: bool = True
    max_tokens: int = 2_000
    schema_tool: bool | None = None
    max_fields: int = 1_000
    max_length: int = 100_000


def mcp_server(
    schema: Any,
    *,
    operations: str | None = None,
    graphql_tool: bool | GraphQLTool = False,
    context_getter: ContextGetter | None = None,
    sync: bool | None = None,
    execute: Execute | None = None,
    name: str = "graphql",
    max_variable_values: int = 10_000,
    **server_options: Any,
) -> MCPServer:
    """An MCP server whose tools are the operations in ``operations``.

    Each named operation in the GraphQL document becomes a tool of that name:
    its description is the tool's description, its variables (with their
    descriptions and defaults) the tool's arguments, and its selection set
    the tool's output schema. Queries are marked read-only and mutations
    destructive, so clients can ask their user before a change. Fragments
    may be shared between operations. The document is validated against
    ``schema`` here, so a schema change that breaks a tool fails at startup.

    ``graphql_tool=True`` (or a :class:`GraphQLTool`) adds a ``graphql``
    tool for operations the client writes itself, and ``graphql_schema`` to
    read the schema they are written against.

    Every operation runs through ``schema`` with the context from
    ``context_getter``, which receives the HTTP request (``None`` over stdio)
    and returns the GraphQL context, sync or async; this is where a bearer
    token becomes your user. It defaults to ``{"request": request}``.

    Operations are awaited with ``schema.execute``. ``sync=True`` runs
    ``schema.execute_sync`` in a worker thread instead, closing Django's
    stale connections around each call; use it for Django's sync ORM or any
    resolver that blocks. ``sync=False`` never falls back, and by default a
    query Django refuses on the event loop is rerun with ``execute_sync``,
    with a warning, while a mutation fails. ``execute(query, variables,
    context, allowed)`` replaces all of this with your own async executor.
    It runs the ``graphql`` tool's operations on your schema rather than the
    one the tool shows, so the tool refuses a field that returns an
    interface or union your schema may resolve to a type the tool leaves
    out.

    A call's variables may hold at most ``max_variable_values`` values,
    counting each object, list and scalar, nested at most 100 levels deep;
    they are measured in a worker thread before GraphQL coerces them. Its
    fields' arguments may hold no more between them, counted as GraphQL
    coerces them, with variables substituted and defaults filled in, for
    each field an alias or fragment repeats, also in that worker thread;
    and as it runs, again each time a list repeats a field: a field that
    would pass the limit fails before its arguments are coerced.

    A tool's result carries each error's message as raised, into the
    client's transcript; add Strawberry's ``MaskErrors`` to ``schema`` to
    mask the exceptions resolvers raise.

    ``server_options`` go to :class:`mcp.server.mcpserver.MCPServer`, for
    example ``token_verifier``, ``auth`` and ``instructions``. A server with
    a mutation tool, or a ``graphql`` tool that may run mutations, warns
    when it is built without ``auth``.
    """
    if operations is None and not graphql_tool:
        raise ValueError("Pass operations=, graphql_tool=, or both.")
    executor = Executor(sync=sync, execute=execute)

    async def build_context(ctx: Context) -> Any:
        request = ctx.request_context.request
        if context_getter is None:
            return {"request": request}
        context = context_getter(request)
        if inspect.isawaitable(context):
            context = await context
        return context

    stored = [] if operations is None else read_operations(schema._schema, operations)
    if graphql_tool:
        taken = sorted({_RUN_TOOL, _SCHEMA_TOOL} & {op.name for op in stored})
        if taken:
            raise ValueError(
                f"The operation names {taken} are taken by graphql_tool's tools."
            )
    target = Target(schema)
    tools = [
        _operation_tool(op, executor, target, build_context, max_variable_values)
        for op in stored
    ]
    server = MCPServer(
        name, tools=[*server_options.pop("tools", ()), *tools], **server_options
    )
    writes = any(op.kind is OperationType.MUTATION for op in stored)
    if graphql_tool:
        options = (
            graphql_tool if isinstance(graphql_tool, GraphQLTool) else GraphQLTool()
        )
        refuses = refuses_introspection(schema)
        if options.schema_tool is None and refuses:
            raise ValueError(
                "The schema refuses introspection, and graphql_schema would give "
                "every MCP client its SDL. Pass GraphQLTool(schema_tool=True) to "
                "give it all the same, or schema_tool=False to leave it out."
            )
        exposed = _add_graphql_tools(
            server,
            schema,
            options,
            executor,
            build_context,
            hide_suggestions=refuses
            or options.schema_tool is False
            or schema.config.disable_field_suggestions,
            max_variable_values=max_variable_values,
            runs_whole_schema=execute is not None,
        )
        writes = writes or exposed.mutation_type is not None
    if writes and server_options.get("auth") is None:
        warnings.warn(_UNAUTHENTICATED_WARNING, UserWarning, stacklevel=2)
    return server


class _Arguments(ArgModelBase):
    """Passes a call's arguments through whole; GraphQL coerces the variables."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        return {"arguments": dict(self.model_extra or {})}


def _operation_tool(
    operation: Operation,
    executor: Executor,
    target: Target,
    build_context: Callable[[Context], Any],
    max_variable_values: int,
) -> Tool:
    properties = operation.input_schema["properties"]

    def prepare(arguments: dict[str, Any]) -> Call:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolError(
                f"{operation.name} has no arguments named {quoted_list(unknown)}."
            )
        variables = {
            key: _parsed(value, properties[key]) for key, value in arguments.items()
        }
        return _prepared(
            target,
            operation.document,
            operation.parsed,
            variables,
            max_values=max_variable_values,
            hide_suggestions=False,
        )

    async def run(arguments: dict[str, Any], ctx: Context) -> CallToolResult:
        call = await asyncio.to_thread(prepare, arguments)
        result = await executor.run(call, await build_context(ctx))
        return _result(result)

    is_query = operation.kind is OperationType.QUERY
    return Tool(
        fn=run,
        name=operation.name,
        description=operation.description,
        parameters=operation.input_schema,
        fn_metadata=FuncMetadata(
            arg_model=_Arguments, output_schema=operation.output_schema
        ),
        is_async=True,
        context_kwarg="ctx",
        annotations=ToolAnnotations(
            read_only_hint=is_query,
            destructive_hint=not is_query,
            open_world_hint=False,
        ),
    )


def _add_graphql_tools(
    server: MCPServer,
    schema: Any,
    options: GraphQLTool,
    executor: Executor,
    build_context: Callable[[Context], Any],
    *,
    hide_suggestions: bool,
    max_variable_values: int,
    runs_whole_schema: bool,
) -> GraphQLSchema:
    """Adds ``graphql`` and ``graphql_schema`` to *server*, and returns the
    schema they show and run operations on.

    With *runs_whole_schema*, operations run on *schema* itself, where an
    interface or union may resolve to a type the tool leaves out, so a field
    that returns one of those is refused.
    """
    exposed = exposed_schema(
        schema._schema,
        allow_mutations=options.allow_mutations,
        root_fields=options.root_fields,
    )
    assert_valid_schema(exposed)
    target = Target(schema, exposed, hide_suggestions=hide_suggestions)
    escaping = escaping_types(schema._schema, exposed) if runs_whole_schema else ()
    schema_tool = options.schema_tool is not False
    allowed = {OperationType.QUERY}
    if options.allow_mutations:
        allowed.add(OperationType.MUTATION)

    def prepare(query: str, variables: dict[str, Any]) -> Call:
        errors, parsed = read_operation(
            exposed,
            query,
            allowed=allowed,
            max_length=options.max_length,
            max_depth=options.max_depth,
            max_tokens=options.max_tokens,
            max_fields=options.max_fields,
            schema_tool=schema_tool,
            hide_suggestions=hide_suggestions,
        )
        if errors:
            raise ToolError("\n".join(errors))
        assert parsed is not None
        field = reaching(exposed, parsed.document, escaping) if escaping else None
        if field is not None:
            raise ToolError(f"{field} may return a type this tool leaves out.")
        return _prepared(
            target,
            query,
            parsed,
            variables,
            max_values=max_variable_values,
            hide_suggestions=hide_suggestions,
        )

    async def graphql(
        query: str, ctx: Context, variables: dict[str, Any] | None = None
    ) -> CallToolResult:
        call = await asyncio.to_thread(prepare, query, variables or {})
        result = await executor.run(call, await build_context(ctx))
        return _result(result)

    kinds = "query or mutation" if options.allow_mutations else "query"
    first = (
        f"Call {_SCHEMA_TOOL} first for the types and fields it may use, and pass "
        if schema_tool
        else "Pass "
    )
    server.add_tool(
        graphql,
        name=_RUN_TOOL,
        description=(
            f"Run one GraphQL {kinds} you write against this API, with your "
            f"permissions. {first}values as variables. Prefer this server's "
            f"other tools when one fits. Nesting is limited to "
            f"{options.max_depth} levels."
        ),
        annotations=ToolAnnotations(
            read_only_hint=not options.allow_mutations,
            destructive_hint=options.allow_mutations,
            open_world_hint=False,
        ),
        structured_output=False,
    )
    if not schema_tool:
        return exposed
    sdl = exposed_sdl(exposed, descriptions=options.descriptions)

    def graphql_schema() -> str:
        return sdl

    server.add_tool(
        graphql_schema,
        name=_SCHEMA_TOOL,
        description=(
            f"The GraphQL schema, as SDL, that the {_RUN_TOOL} tool's "
            "operations are written against."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
        structured_output=False,
    )
    return exposed


def _prepared(
    target: Target,
    query: str,
    parsed: Parsed,
    variables: dict[str, Any],
    *,
    max_values: int,
    hide_suggestions: bool,
) -> Call:
    """A call of *parsed* with *variables*, which may hold at most
    *max_values* values, as may its fields' arguments between them.

    Variables that do not fit the types the operation declares are refused
    here with ``hide_suggestions``, and otherwise reach execution, which
    reports them before any field runs.
    """
    errors = check_variables(variables, max_values=max_values)
    if errors:
        raise ToolError("\n".join(errors))
    assert target.graphql_schema is not None
    coerced = get_variable_values(
        target.graphql_schema,
        parsed.operation.variable_definitions or (),
        variables,
        hide_suggestions=hide_suggestions,
    )
    if isinstance(coerced, list):
        if hide_suggestions:
            raise ToolError("\n".join(error.message for error in coerced))
        return Call(query, parsed.document, parsed.kind, variables, target)
    costs = Costs(target.graphql_schema, coerced, max_values)
    errors = costs.check(parsed)
    if errors:
        raise ToolError("\n".join(errors))
    return Call(query, parsed.document, parsed.kind, variables, target, costs)


def _parsed(value: Any, schema: dict[str, Any]) -> Any:
    """*value*, decoded if a client sent an object or list as a JSON string.

    Some clients do, Claude Desktop among them.
    """
    if not isinstance(value, str):
        return value
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    structured = "$ref" in schema or "anyOf" in schema or "array" in types
    if not structured:
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return decoded if isinstance(decoded, dict | list) else value


def _result(result: Any) -> CallToolResult:
    content: dict[str, Any] = {"data": result.data}
    if result.errors:
        content["errors"] = [error.formatted for error in result.errors]
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(content))],
        structured_content=content,
        is_error=result.data is None and bool(result.errors),
    )
