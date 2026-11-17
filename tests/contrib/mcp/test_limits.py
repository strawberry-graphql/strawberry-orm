"""What bounds the work one call can ask for: the fields an operation selects
once its fragments are expanded, and the size of its variables."""

from __future__ import annotations

import threading

import pytest
import strawberry
from mcp import Client
from strawberry.types.graphql import OperationType

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server
from strawberry_orm.contrib.mcp.checks import check_operation

from .fakes import schema


@strawberry.type
class Person:
    name: str

    @strawberry.field
    def friends(self) -> list[Person]:
        return []


@strawberry.type
class PeopleQuery:
    @strawberry.field
    def people(self) -> list[Person]:
        return [Person(name="Ann")]


people = strawberry.Schema(query=PeopleQuery)

LIST_POSTS = "query ListPosts($filter: PostFilter) { posts(filter: $filter) { title } }"
FILTERED = "query($f: PostFilter) { posts(filter: $f) { title } }"


def _aliases_through_fragments(aliases: int, levels: int) -> str:
    """``friends`` under *aliases* names in each of *levels* fragments, each
    spreading the next: short, shallow, and no field repeated in one place."""
    fragments = [
        f"fragment F{i} on Person {{ "
        + " ".join(f"a{j}: friends {{ ...F{i + 1} }}" for j in range(aliases))
        + " }"
        for i in range(levels - 1)
    ]
    last = f"fragment F{levels - 1} on Person {{ name }}"
    return "{ people { ...F0 } } " + " ".join([*fragments, last])


def _fields(query: str, max_fields: int) -> list[str]:
    errors, _ = check_operation(
        people._schema,
        query,
        allowed={OperationType.QUERY},
        max_depth=10,
        max_tokens=2_000,
        max_fields=max_fields,
    )
    return errors


def _filter(branches: int) -> dict:
    """A filter of ``2 + 2 * branches`` values."""
    return {"anyOf": [{"title": "Hello"}] * branches}


# Fields, fragments expanded


async def test_aliases_spread_through_fragments_count_toward_the_field_budget():
    query = _aliases_through_fragments(3, 8)

    async with Client(mcp_server(people, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert len(query) < 800
    assert result.is_error is True
    assert (
        "The query selects 5467 fields, fragments included; the limit is 1000."
        in result.content[0].text
    )


async def test_the_field_budget_can_be_raised():
    server = mcp_server(people, graphql_tool=GraphQLTool(max_fields=5_467))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": _aliases_through_fragments(3, 8)}
        )

    assert result.structured_content == {
        "data": {"people": [{"a0": [], "a1": [], "a2": []}]}
    }


def test_a_fragment_counts_wherever_it_is_spread():
    query = (
        "{ people { ...P friends { ...P } } } "
        "fragment P on Person { name friends { name } }"
    )

    assert _fields(query, 8) == []
    assert _fields(query, 7) == [
        "The query selects 8 fields, fragments included; the limit is 7."
    ]


def test_a_fragment_spread_twice_in_one_place_counts_once():
    query = (
        "{ people { ...P ... on Person { ...P } } } "
        "fragment P on Person { name friends { name } }"
    )

    assert _fields(query, 4) == []


def test_every_alias_and_typename_counts():
    query = "{ people { __typename a: name b: name } other: people { name } }"

    assert _fields(query, 5) == [
        "The query selects 6 fields, fragments included; the limit is 5."
    ]


def test_a_fragment_cycle_is_counted_once_and_refused_by_validation():
    query = (
        "{ people { ...A } } "
        "fragment A on Person { friends { ...B } } "
        "fragment B on Person { friends { ...A } }"
    )

    assert _fields(query, 1_000) == [
        "Cannot spread fragment 'A' within itself via 'B'."
    ]


# Variables


async def test_the_graphql_tool_refuses_variables_past_their_budget():
    async with Client(mcp_server(schema, graphql_tool=True)) as client:
        result = await client.call_tool(
            "graphql", {"query": FILTERED, "variables": {"f": _filter(5_000)}}
        )

    assert result.is_error is True
    assert (
        "The variables hold over 10000 values; the limit is 10000."
        in result.content[0].text
    )


async def test_a_stored_operation_refuses_arguments_past_their_budget():
    async with Client(mcp_server(schema, operations=LIST_POSTS)) as client:
        result = await client.call_tool("ListPosts", {"filter": _filter(5_000)})

    assert result.is_error is True
    assert (
        "The variables hold over 10000 values; the limit is 10000."
        in result.content[0].text
    )


async def test_variables_up_to_the_budget_still_run():
    async with Client(mcp_server(schema, operations=LIST_POSTS)) as client:
        result = await client.call_tool("ListPosts", {"filter": _filter(4_999)})

    assert result.structured_content == {"data": {"posts": [{"title": "Hello"}]}}


@pytest.mark.parametrize(
    ("variables", "refused"),
    [
        ({"filter": {"title": "Hello", "drafts": False}}, False),
        ({"filter": {"title": "Hello", "drafts": False, "anyOf": None}}, True),
        ({"filter": {"anyOf": [{"anyOf": []}]}}, True),
    ],
    ids=["at the budget", "one past it", "nested"],
)
async def test_every_object_list_and_scalar_counts(variables, refused):
    server = mcp_server(schema, operations=LIST_POSTS, max_variable_values=3)

    async with Client(server) as client:
        result = await client.call_tool("ListPosts", variables)

    assert result.is_error is refused
    if refused:
        assert "The variables hold over 3 values; the limit is 3." in (
            result.content[0].text
        )


def _nested(levels: int) -> list:
    value: list = []
    for _ in range(levels - 1):
        value = [value]
    return value


@pytest.mark.parametrize(("levels", "refused"), [(100, False), (101, True)])
async def test_variables_nested_too_deeply_are_refused(levels, refused):
    async with Client(mcp_server(schema, graphql_tool=True)) as client:
        result = await client.call_tool(
            "graphql", {"query": FILTERED, "variables": {"f": _nested(levels)}}
        )

    text = result.content[0].text
    assert result.is_error is True
    assert (
        "The variables are nested over 100 levels deep; the limit is 100." in text
    ) is refused


async def test_variables_are_measured_off_the_event_loop(monkeypatch):
    from strawberry_orm.contrib.mcp import server as module

    threads = []
    measure = module.check_variables

    def recording(*args, **kwargs):
        threads.append(threading.current_thread())
        return measure(*args, **kwargs)

    monkeypatch.setattr(module, "check_variables", recording)
    server = mcp_server(schema, operations=LIST_POSTS, graphql_tool=True)
    async with Client(server) as client:
        await client.call_tool("graphql", {"query": "{ named { name } }"})
        await client.call_tool("ListPosts", {})

    assert len(threads) == 2
    assert threading.main_thread() not in threads
