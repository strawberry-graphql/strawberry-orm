"""The MCP server: stored operations as tools, and the optional graphql tool."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import strawberry
from graphql import NoSchemaIntrospectionCustomRule, ValidationRule
from graphql.language.parser import Parser
from mcp import Client
from mcp.server.mcpserver.tools import Tool
from strawberry.extensions import (
    AddValidationRules,
    DisableIntrospection,
    MaskErrors,
    MaxTokensLimiter,
    ParserCache,
    QueryDepthLimiter,
    ValidationCache,
)
from strawberry.types.graphql import OperationType

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server
from strawberry_orm.contrib.mcp.checks import (
    check_operation,
    read_operation,
    refuses_introspection,
)

from .fakes import OPERATIONS, RUNS, Mutation, Query, relay_schema, schema

HELLO = {
    "title": "Hello",
    "status": "PUBLISHED",
    "published": "2026-01-02",
    "author": {"name": "Alice"},
}


def _server(**options):
    options.setdefault("operations", OPERATIONS)
    return mcp_server(schema, **options)


async def _tools(server):
    async with Client(server) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


# Stored operations


async def test_each_operation_is_a_tool():
    tools = await _tools(_server())

    assert list(tools) == [
        "ListPosts",
        "FindPost",
        "Search",
        "WhoIsNamed",
        "Secrets",
        "Missing",
        "Publish",
    ]
    assert tools["ListPosts"].description == (
        "Published posts, or drafts too when asked."
    )
    assert tools["FindPost"].description == "Runs the GraphQL query FindPost."


async def test_queries_are_read_only_and_mutations_destructive():
    tools = await _tools(_server())

    query, mutation = tools["ListPosts"].annotations, tools["Publish"].annotations
    assert (query.read_only_hint, query.destructive_hint) == (True, False)
    assert (mutation.read_only_hint, mutation.destructive_hint) == (False, True)
    assert query.open_world_hint is mutation.open_world_hint is False


async def test_a_tool_publishes_its_variables_and_its_data():
    tools = await _tools(_server())

    listed = tools["ListPosts"]
    assert listed.input_schema["properties"]["first"] == {
        "type": ["integer", "null"],
        "default": 10,
    }
    assert listed.output_schema["required"] == ["data"]
    posts = listed.output_schema["properties"]["data"]["properties"]["posts"]
    assert posts["items"]["required"] == ["title", "status", "published", "author"]


async def test_a_call_runs_the_operation_with_its_arguments():
    async with Client(_server()) as client:
        result = await client.call_tool(
            "ListPosts", {"filter": {"title": "Hello"}, "first": 1}
        )

    assert result.is_error is False
    assert result.structured_content == {"data": {"posts": [HELLO]}}
    assert json.loads(result.content[0].text) == result.structured_content


async def test_defaults_apply_to_arguments_left_out():
    async with Client(_server()) as client:
        result = await client.call_tool("ListPosts", {})

    assert result.structured_content == {"data": {"posts": [HELLO]}}


async def test_an_object_sent_as_a_json_string_is_decoded():
    async with Client(_server()) as client:
        result = await client.call_tool(
            "ListPosts", {"filter": json.dumps({"drafts": True})}
        )

    assert [p["title"] for p in result.structured_content["data"]["posts"]] == [
        "Hello",
        "Draft",
    ]


@pytest.mark.parametrize("value", ["not json", "[1", '"quoted"', "7"])
async def test_a_string_that_is_not_an_object_reaches_graphql_as_it_is(value):
    async with Client(_server()) as client:
        result = await client.call_tool("ListPosts", {"filter": value})

    assert result.is_error is True
    assert "PostFilter" in result.structured_content["errors"][0]["message"]


async def test_a_plain_string_argument_is_never_decoded():
    async with Client(_server()) as client:
        result = await client.call_tool("Search", {"text": '{"a": 1}'})

    tag = result.structured_content["data"]["search"][1]
    assert tag == {"__typename": "Tag", "label": '{"a": 1}'}


async def test_an_unknown_argument_is_refused():
    async with Client(_server()) as client:
        result = await client.call_tool("ListPosts", {"nope": 1})

    assert result.is_error is True
    assert "ListPosts has no arguments named ['nope']" in result.content[0].text


async def test_unknown_arguments_are_named_cut_short():
    names = {f"{n}{'x' * 5_000}": 1 for n in range(100)}
    async with Client(_server()) as client:
        result = await client.call_tool("ListPosts", names)

    assert result.is_error is True
    text = result.content[0].text
    assert "ListPosts has no arguments named ['0xxx" in text, text[:200]
    assert text.endswith(", and 95 more]."), text[-200:]
    assert len(text) <= 400, len(text)


async def test_invalid_variables_are_a_tool_error():
    async with Client(_server()) as client:
        result = await client.call_tool("FindPost", {"key": {}, "withStatus": True})

    assert result.is_error is True
    [error] = result.structured_content["errors"]
    assert "exactly one key" in error["message"]
    assert result.structured_content["data"] is None


async def test_partial_data_comes_back_with_its_errors():
    async with Client(_server()) as client:
        result = await client.call_tool("Secrets", {})

    assert result.is_error is False
    data, errors = (
        result.structured_content["data"],
        result.structured_content["errors"],
    )
    assert data["posts"][0] == {"title": "Hello", "secret": None}
    assert errors[0]["message"] == "You may not read secrets."
    assert errors[0]["path"] == ["posts", 0, "secret"]


async def test_no_data_at_all_is_a_tool_error():
    async with Client(_server()) as client:
        result = await client.call_tool("Missing", {})

    assert result.is_error is True
    assert result.structured_content["errors"][0]["message"] == "No such thing."


async def test_mask_errors_hides_what_resolvers_raise_and_keeps_graphqls_own():
    masked = strawberry.Schema(
        query=Query,
        mutation=Mutation,
        extensions=[
            lambda: MaskErrors(
                should_mask_error=lambda error: error.original_error is not None
            )
        ],
    )
    async with Client(mcp_server(masked, operations=OPERATIONS)) as client:
        raised = await client.call_tool("Secrets", {})
        coerced = await client.call_tool("FindPost", {"key": {}, "withStatus": True})

    assert raised.structured_content["errors"][0]["message"] == "Unexpected error."
    assert "secrets" not in raised.content[0].text
    assert "exactly one key" in coerced.structured_content["errors"][0]["message"]


async def test_a_mutation_tool_runs_its_mutation_once():
    before = RUNS["publish"]

    async with Client(_server()) as client:
        result = await client.call_tool("Publish", {"title": "New"})

    assert result.structured_content == {"data": {"publish": {"title": "New"}}}
    assert RUNS["publish"] == before + 1


async def test_the_executed_document_has_only_the_fragments_it_uses():
    seen = []

    async def execute(query, variables, context, allowed):
        seen.append(query)
        return await schema.execute(query, variable_values=variables)

    async with Client(_server(execute=execute)) as client:
        await client.call_tool("ListPosts", {})
        await client.call_tool("Search", {"text": "x"})

    listed, searched = seen
    assert "fragment PostFields on Post" in listed
    assert "Only posts matching this" not in listed
    assert "fragment" not in searched


# Context


async def test_the_context_comes_from_the_request():
    contexts = []

    def sync_getter(request):
        contexts.append({"user": "sync", "request": request})
        return contexts[-1]

    async def async_getter(request):
        contexts.append({"user": "async"})
        return contexts[-1]

    for getter in (sync_getter, async_getter):
        async with Client(_server(context_getter=getter)) as client:
            await client.call_tool("ListPosts", {})

    assert [c["seen"] for c in contexts] == [["sync"], ["async"]]
    assert contexts[0]["request"] is None


async def test_the_default_context_holds_the_request():
    seen = []

    async def execute(query, variables, context, allowed):
        seen.append(context)
        return await schema.execute(query, variable_values=variables)

    async with Client(_server(execute=execute)) as client:
        await client.call_tool("Missing", {})

    assert seen == [{"request": None}]


# Building the server


@pytest.mark.parametrize(
    ("operations", "error"),
    [
        ("query {", "do not parse"),
        ("query A { nope }", "Cannot query field 'nope'"),
        ("{ missing }", "needs a name"),
        ("fragment F on Post { title }", "do not validate"),
        ("", "do not parse"),
    ],
)
def test_a_broken_document_fails_when_the_server_is_built(operations, error):
    with pytest.raises(ValueError, match=error):
        _server(operations=operations)


def test_subscriptions_cannot_be_tools():
    @strawberry.type
    class Subscription:
        @strawberry.subscription
        async def ticks(self) -> AsyncGenerator[int]:  # pragma: no cover
            yield 1

    streaming = strawberry.Schema(query=Query, subscription=Subscription)

    with pytest.raises(ValueError, match="Ticks is a subscription"):
        mcp_server(streaming, operations="subscription Ticks { ticks }")


def test_a_server_needs_some_tool():
    with pytest.raises(ValueError, match="operations=, graphql_tool=, or both"):
        mcp_server(schema)


@pytest.mark.parametrize("name", ["graphql", "graphql_schema"])
def test_operations_cannot_take_the_graphql_tools_names(name):
    with pytest.raises(ValueError, match="taken by graphql_tool"):
        mcp_server(schema, operations=f"query {name} {{ missing }}", graphql_tool=True)


async def test_an_operation_may_use_a_graphql_tool_name_without_graphql_tool():
    tools = await _tools(mcp_server(schema, operations="query graphql { missing }"))

    assert list(tools) == ["graphql"]


async def test_tools_of_your_own_are_kept():
    def ping() -> str:
        return "pong"

    tools = await _tools(_server(tools=[Tool.from_function(ping)], name="blog"))

    assert "ping" in tools
    assert "ListPosts" in tools


# The graphql tool


async def test_the_graphql_tool_runs_a_query_the_client_writes():
    server = _server(operations=None, graphql_tool=True)

    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(
            "graphql",
            {
                "query": "query($t: String) { posts(filter: {title: $t}) { title } }",
                "variables": {"t": "Hello"},
            },
        )

    assert list(tools) == ["graphql", "graphql_schema"]
    assert tools["graphql"].annotations.read_only_hint is True
    assert "Call graphql_schema first" in tools["graphql"].description
    assert "limited to 10 levels" in tools["graphql"].description
    assert result.structured_content == {"data": {"posts": [{"title": "Hello"}]}}


async def test_the_graphql_tool_without_variables():
    async with Client(_server(graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": "{ named { name } }"})

    assert result.structured_content == {"data": {"named": {"name": "Alice"}}}


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ('mutation { publish(title: "x") { title } }', "Mutation operations are not"),
        ("{ named { name } } { missing }", "exactly one operation"),
        ("{ nope }", "Cannot query field 'nope'"),
        ("{ posts { author { name } } }", "3 levels deep; the limit is 2"),
        (
            "{ posts { ...P } } fragment P on Post { ... on Post { author { name } } }",
            "3 levels deep; the limit is 2",
        ),
        ("{ posts { ...Nope } }", "Unknown fragment 'Nope'"),
        (
            "{ posts { ...A } } fragment A on Post { ...B } fragment B on Post { ...A }",
            "Cannot spread fragment",
        ),
        ("{ posts", "Syntax Error"),
    ],
)
async def test_the_graphql_tool_checks_an_operation_before_it_runs(query, error):
    server = _server(graphql_tool=GraphQLTool(max_depth=2))

    async with Client(server) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.is_error is True
    assert error in result.content[0].text


def _doubling_fragments(levels: int) -> str:
    """One selection of posts, through fragments that each spread the next twice."""
    spreads = " ".join(
        f"fragment F{i} on Post {{ ...F{i + 1} ...F{i + 1} }}" for i in range(levels)
    )
    return f"{{ posts {{ ...F0 }} }} {spreads} fragment F{levels} on Post {{ title }}"


def test_a_fragment_spread_many_times_is_measured_once():
    started = time.perf_counter()
    errors, kind = check_operation(
        schema._schema,
        _doubling_fragments(22),
        allowed={OperationType.QUERY},
        max_depth=2,
        max_tokens=2_000,
        max_fields=1_000,
    )
    assert time.perf_counter() - started < 1
    assert (errors, kind) == ([], OperationType.QUERY)


async def test_the_graphql_tool_refuses_a_document_past_its_token_limit():
    server = _server(graphql_tool=GraphQLTool(max_tokens=8))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": "{ posts { title author { name } } }"}
        )

    assert result.is_error is True
    assert "Document contains more than 8 tokens." in result.content[0].text


async def test_the_graphql_tool_checks_an_operation_off_the_event_loop(monkeypatch):
    from strawberry_orm.contrib.mcp import server as module

    threads = []

    def recording(*args, **kwargs):
        threads.append(threading.current_thread())
        return read_operation(*args, **kwargs)

    monkeypatch.setattr(module, "read_operation", recording)
    async with Client(_server(graphql_tool=True)) as client:
        await client.call_tool("graphql", {"query": "{ named { name } }"})

    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()


async def test_the_graphql_tool_parses_and_validates_off_the_event_loop(monkeypatch):
    loop = threading.current_thread()
    seen = []

    class Recording(ValidationRule):
        def enter_document(self, *_):
            seen.append(("validate", threading.current_thread() is loop))

    parse_document = Parser.parse_document

    def recording(self):
        seen.append(("parse", threading.current_thread() is loop))
        return parse_document(self)

    recorded = strawberry.Schema(
        query=Query, extensions=[lambda: AddValidationRules([Recording])]
    )
    server = mcp_server(
        recorded, operations="query Named { named { name } }", graphql_tool=True
    )
    monkeypatch.setattr(Parser, "parse_document", recording)
    async with Client(server) as client:
        await client.call_tool("graphql", {"query": "{ named { name } }"})
        written, seen[:] = seen[:], []
        await client.call_tool("Named", {})

    assert written == [("parse", False), ("validate", False)]
    assert seen == [("validate", False)]


@pytest.mark.parametrize(
    ("extension", "error"),
    [
        (lambda: MaxTokensLimiter(max_token_count=5), "more than 5 tokens"),
        (lambda: QueryDepthLimiter(max_depth=1), "exceeds maximum operation depth"),
    ],
)
async def test_the_schemas_own_extensions_still_check_the_graphql_tool(
    extension, error
):
    limited = strawberry.Schema(query=Query, extensions=[extension])

    async with Client(mcp_server(limited, graphql_tool=True)) as client:
        result = await client.call_tool(
            "graphql", {"query": "{ posts { title author { name } } }"}
        )

    assert result.is_error is True
    assert error in result.content[0].text


async def test_the_graphql_tool_runs_with_the_schemas_own_caches():
    cached = strawberry.Schema(query=Query, extensions=[ParserCache, ValidationCache])

    async with Client(mcp_server(cached, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": "{ named { name } }"})

    assert result.structured_content == {"data": {"named": {"name": "Alice"}}}


@pytest.mark.parametrize(
    ("query", "times"),
    [
        ("{ posts { " + "author { name } " * 447 + "} }", 447),
        ("{ posts { " + "author { name name name name name } " * 5 + "} }", 25),
        (
            "{ posts { ...A ...B ...C } } "
            + " ".join(f"fragment {f} on Post {{ {'title ' * 7}}}" for f in "ABC"),
            21,
        ),
        ("{ posts { " + "... on Post { title title title } " * 7 + "} }", 21),
    ],
    ids=["siblings", "nested", "fragments", "inline fragments"],
)
async def test_the_graphql_tool_refuses_a_field_merged_too_often(query, times):
    async with Client(_server(graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.is_error is True
    assert (
        f"The query selects a field up to {times} times in one place; "
        "the limit is 20." in result.content[0].text
    )


async def test_a_field_merged_up_to_the_limit_still_runs():
    query = (
        "{ posts { " + "author { name } " * 20 + "...F " * 30 + "} } "
        "fragment F on Post { title }"
    )

    async with Client(_server(graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.structured_content == {
        "data": {"posts": [{"author": {"name": "Alice"}, "title": "Hello"}]}
    }


async def test_the_graphql_tool_may_run_mutations_when_allowed():
    server = _server(operations=None, graphql_tool=GraphQLTool(allow_mutations=True))

    async with Client(server) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["graphql"]
        result = await client.call_tool(
            "graphql", {"query": 'mutation { publish(title: "x") { title } }'}
        )
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert result.structured_content == {"data": {"publish": {"title": "x"}}}
    assert (tool.annotations.read_only_hint, tool.annotations.destructive_hint) == (
        False,
        True,
    )
    assert "query or mutation" in tool.description
    assert "type Mutation" in sdl


async def test_the_schema_tool_returns_what_the_graphql_tool_may_use():
    async with Client(_server(operations=None, graphql_tool=True)) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["graphql_schema"]
        result = await client.call_tool("graphql_schema", {})

    sdl = result.content[0].text
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert "type Query {" in sdl
    assert "type Mutation" not in sdl
    assert "Where a post is in its life." in sdl
    assert result.structured_content is None


async def test_root_fields_limit_what_the_graphql_tool_shows_and_runs():
    server = _server(operations=None, graphql_tool=GraphQLTool(root_fields=["named"]))

    async with Client(server) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        refused = await client.call_tool("graphql", {"query": "{ missing }"})

    assert "type Author implements Named" in sdl
    assert "type Post " not in sdl
    assert refused.is_error is True
    assert "Cannot query field 'missing'" in refused.content[0].text


async def test_root_fields_leave_out_types_that_only_share_an_interface():
    server = mcp_server(relay_schema, graphql_tool=GraphQLTool(root_fields=["notes"]))
    query = "{ notes { ... on Node { ... on AuditEntry { actorEmail } } } }"

    async with Client(server) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        refused = await client.call_tool("graphql", {"query": query})

    assert "type Note implements Node" in sdl
    assert "interface Node" in sdl
    for hidden in ("AuditEntry", "SigningKey", "privatePem", "auditLog"):
        assert hidden not in sdl
    assert refused.is_error is True
    assert "Unknown type 'AuditEntry'" in refused.content[0].text


async def test_a_field_returning_an_interface_reaches_its_implementations():
    server = mcp_server(relay_schema, graphql_tool=GraphQLTool(root_fields=["node"]))
    query = '{ node(id: "Tm90ZTox") { id ... on Note { text } } }'

    async with Client(server) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        result = await client.call_tool("graphql", {"query": query})

    assert "type Note implements Node" in sdl
    assert "type AuditEntry implements Node" in sdl
    assert result.structured_content == {
        "data": {"node": {"id": "Tm90ZTox", "text": "Hi"}}
    }


async def test_root_fields_may_name_allowed_mutations():
    options = GraphQLTool(allow_mutations=True, root_fields=["named", "publish"])

    async with Client(_server(operations=None, graphql_tool=options)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert "publish(title: String!): Post!" in sdl
    assert "missing" not in sdl


async def test_root_fields_without_a_mutation_leave_out_the_mutation_type():
    options = GraphQLTool(allow_mutations=True, root_fields=["named"])

    async with Client(_server(operations=None, graphql_tool=options)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert "type Mutation" not in sdl


async def test_the_schema_can_leave_out_descriptions():
    options = GraphQLTool(descriptions=False)

    async with Client(_server(operations=None, graphql_tool=options)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert "Where a post is in its life." not in sdl
    assert "enum Status" in sdl


@pytest.mark.parametrize(
    "refusing",
    [
        DisableIntrospection,
        lambda: AddValidationRules([NoSchemaIntrospectionCustomRule]),
    ],
    ids=["DisableIntrospection", "a factory of the rule"],
)
def test_a_schema_that_refuses_introspection_must_say_to_share_it(refusing):
    closed = strawberry.Schema(query=Query, mutation=Mutation, extensions=[refusing])
    with pytest.raises(ValueError, match="refuses introspection"):
        mcp_server(closed, graphql_tool=True)


def test_a_schema_without_strawberrys_extensions_refuses_nothing():
    assert not refuses_introspection(SimpleNamespace(_schema=schema._schema))
    assert not refuses_introspection(schema)


async def test_a_schema_that_refuses_introspection_can_leave_its_sdl_out():
    closed = strawberry.Schema(
        query=Query, mutation=Mutation, extensions=[DisableIntrospection]
    )
    server = mcp_server(closed, graphql_tool=GraphQLTool(schema_tool=False))

    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        ran = await client.call_tool(
            "graphql", {"query": "{ posts(first: 1) { title } }"}
        )
        asked = await client.call_tool(
            "graphql", {"query": "{ __schema { types { name } } }"}
        )

    assert list(tools) == ["graphql"]
    assert "graphql_schema" not in tools["graphql"].description
    assert ran.structured_content == {"data": {"posts": [{"title": "Hello"}]}}
    assert asked.is_error
    assert asked.content[0].text.endswith(": Introspection is not available here.")


async def test_a_schema_that_refuses_introspection_can_share_its_sdl_anyway():
    closed = strawberry.Schema(
        query=Query, mutation=Mutation, extensions=[DisableIntrospection]
    )
    server = mcp_server(closed, graphql_tool=GraphQLTool(schema_tool=True))

    async with Client(server) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert "type Query {" in sdl


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (GraphQLTool(root_fields=["named", "nope"]), r"field: \['nope'\] \(mutations"),
        (GraphQLTool(root_fields=["publish"]), "mutations need allow_mutations"),
        (
            GraphQLTool(allow_mutations=True, root_fields=["nope", "named"]),
            r"field: \['nope'\]\.$",
        ),
        (GraphQLTool(allow_mutations=True, root_fields=["publish"]), "query field"),
    ],
)
def test_root_fields_are_checked(options, error):
    with pytest.raises(ValueError, match=error):
        _server(graphql_tool=options)
