"""Serve a Strawberry schema's operations as MCP tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import warnings
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from graphql import GraphQLSchema
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict
from strawberry.types.graphql import OperationType

from .checks import (
    check_coercion,
    check_operation,
    check_variables,
    exposed_schema,
    exposed_sdl,
    refuses_introspection,
)
from .execution import Execute, Executor
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

    Only queries run unless ``allow_mutations``. ``root_fields`` limits the
    root fields it may use, and the schema shows only those and the types
    they reach, through any field that returns a root type too. An operation
    must be a single operation of at most ``max_tokens`` tokens and
    ``max_depth`` levels that passes validation, may not select one field
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

    The token limit is checked as the operation is parsed, before anything
    else reads it; the schema's own extensions, such as Strawberry's
    ``MaxTokensLimiter``, only run once it executes. Checking the operation,
    and the parsing and validation ``schema.execute`` does before its
    resolvers run, happen in a worker thread, unless an extension of the
    schema's own, such as ``ParserCache`` or ``ValidationCache``, does them.
    """

    allow_mutations: bool = False
    root_fields: Collection[str] | None = None
    max_depth: int = 10
    descriptions: bool = True
    max_tokens: int = 2_000
    schema_tool: bool | None = None
    max_fields: int = 1_000


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

    A call's variables may hold at most ``max_variable_values`` values,
    counting each object, list and scalar, nested at most 100 levels deep;
    they are measured in a worker thread before GraphQL coerces them.

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
    executor = Executor(schema, sync=sync, execute=execute)

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
    tools = [
        _operation_tool(op, executor, build_context, max_variable_values)
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
            schema._schema,
            options,
            executor,
            build_context,
            hide_suggestions=refuses
            or options.schema_tool is False
            or schema.config.disable_field_suggestions,
            max_variable_values=max_variable_values,
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
    build_context: Callable[[Context], Any],
    max_variable_values: int,
) -> Tool:
    properties = operation.input_schema["properties"]

    async def run(arguments: dict[str, Any], ctx: Context) -> CallToolResult:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolError(f"{operation.name} has no arguments named {unknown}.")
        variables = {
            key: _parsed(value, properties[key]) for key, value in arguments.items()
        }
        errors = await asyncio.to_thread(
            check_variables, variables, max_values=max_variable_values
        )
        if errors:
            raise ToolError("\n".join(errors))
        result = await executor.run(
            operation.document, variables, await build_context(ctx), {operation.kind}
        )
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
    schema: GraphQLSchema,
    options: GraphQLTool,
    executor: Executor,
    build_context: Callable[[Context], Any],
    *,
    hide_suggestions: bool,
    max_variable_values: int,
) -> GraphQLSchema:
    """Adds ``graphql`` and ``graphql_schema`` to *server*, and returns the
    schema they show."""
    allowlist = None if options.root_fields is None else set(options.root_fields)
    if allowlist is not None:
        _check_root_fields(schema, allowlist, allow_mutations=options.allow_mutations)
    exposed = exposed_schema(
        schema, allow_mutations=options.allow_mutations, root_fields=allowlist
    )
    schema_tool = options.schema_tool is not False
    allowed = {OperationType.QUERY}
    if options.allow_mutations:
        allowed.add(OperationType.MUTATION)

    def check(
        query: str, variables: dict[str, Any]
    ) -> tuple[list[str], OperationType | None]:
        errors, kind = check_operation(
            exposed,
            query,
            allowed=allowed,
            max_depth=options.max_depth,
            max_tokens=options.max_tokens,
            max_fields=options.max_fields,
            schema_tool=schema_tool,
            hide_suggestions=hide_suggestions,
        )
        errors = errors or check_variables(variables, max_values=max_variable_values)
        if not errors and hide_suggestions:
            errors = check_coercion(exposed, query, variables)
        return errors, kind

    async def graphql(
        query: str, ctx: Context, variables: dict[str, Any] | None = None
    ) -> CallToolResult:
        variables = variables or {}
        errors, kind = await asyncio.to_thread(check, query, variables)
        if errors:
            raise ToolError("\n".join(errors))
        assert kind is not None
        result = await executor.run(
            query,
            variables,
            await build_context(ctx),
            {kind},
            prepare_off_loop=True,
        )
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


def _check_root_fields(
    schema: GraphQLSchema, allowlist: set[str], *, allow_mutations: bool
) -> None:
    assert schema.query_type is not None
    queries = set(schema.query_type.fields)
    mutation = schema.mutation_type
    mutations = set(mutation.fields) if allow_mutations and mutation else set()
    unknown = sorted(allowlist - queries - mutations)
    if unknown:
        hint = "" if allow_mutations else " (mutations need allow_mutations)"
        raise ValueError(f"root_fields names no such root field: {unknown}{hint}.")
    if not allowlist & queries:
        raise ValueError("root_fields must include at least one query field.")


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
