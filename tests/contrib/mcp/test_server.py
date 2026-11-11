"""The MCP server: stored operations as tools, and the optional graphql tool."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest
import strawberry
from mcp import Client
from mcp.server.mcpserver.tools import Tool

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server

from .fakes import OPERATIONS, RUNS, Query, schema

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
