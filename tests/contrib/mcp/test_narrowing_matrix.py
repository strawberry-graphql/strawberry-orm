"""What the graphql tool leaves out of its schema stays out of what it runs:
without ``allow_mutations`` no field, however it is reached, produces a
value of the Mutation or Subscription type, and ``root_fields`` narrows each
root type by its own names."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import pytest
import strawberry
from django.core.exceptions import SynchronousOnlyOperation
from mcp import Client
from strawberry import relay

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server

RAN: list[str] = []


@pytest.fixture(autouse=True)
def _forget_runs():
    RAN.clear()
    logger = logging.getLogger("strawberry.execution")
    disabled = logger.disabled
    logger.disabled = True
    yield
    logger.disabled = disabled


@strawberry.interface
class Resettable:
    reset: bool


@strawberry.interface
class Labelled:
    label: str


@strawberry.type
class Note:
    text: str = "Hi"


@strawberry.type
class Mutation(Resettable):
    @strawberry.mutation
    def reset(self) -> bool:
        RAN.append("Mutation.reset")
        return True

    @strawberry.mutation
    def publish(self) -> bool:
        RAN.append("Mutation.publish")
        return True


@strawberry.type
class Subscription(Resettable):
    @strawberry.subscription
    async def reset(self) -> AsyncGenerator[bool, None]:
        RAN.append("Subscription.reset")
        yield True


Anything = Annotated[Note | Mutation | Subscription, strawberry.union("Anything")]


@strawberry.type
class Payload:
    @strawberry.field
    def mutation(self) -> Mutation:
        return Mutation()

    @strawberry.field
    def mutations(self) -> list[Mutation]:
        return [Mutation()]

    @strawberry.field
    def subscription(self) -> Subscription:
        return Subscription()

    @strawberry.field
    def resettable(self) -> Resettable | None:
        return Mutation()

    @strawberry.field
    def anything(self) -> list[Anything]:
        return [Mutation()]


@strawberry.type
class Query:
    @strawberry.field
    def hello(self) -> str:
        return "hi"

    @strawberry.field
    def maintenance(self) -> Resettable | None:
        return Mutation()

    @strawberry.field
    def resettables(self) -> list[Resettable] | None:
        return [Mutation()]

    @strawberry.field
    def ticker(self) -> Resettable | None:
        return Subscription()

    @strawberry.field
    def anything(self) -> Anything | None:
        return Mutation()

    @strawberry.field
    def payload(self) -> Payload:
        return Payload()


roots = strawberry.Schema(query=Query, mutation=Mutation, subscription=Subscription)


@strawberry.type(name="Query")
class LabelledQuery(Labelled):
    @strawberry.field
    def label(self) -> str:
        RAN.append("Query.label")
        return "root"

    @strawberry.field
    def hello(self) -> str:
        return "hi"

    @strawberry.field
    def labelled(self) -> Labelled | None:
        return LabelledQuery()


labelled = strawberry.Schema(query=LabelledQuery)


@strawberry.type(name="Mutation")
class NodeMutation(relay.Node):
    id: relay.NodeID[int]

    @strawberry.mutation
    def wipe(self) -> bool:
        RAN.append("Mutation.wipe")
        return True

    @classmethod
    def resolve_nodes(cls, *, info, node_ids, required=False):
        return [cls(id=int(pk)) for pk in node_ids]


@strawberry.type(name="Query")
class NodeQuery:
    node: relay.Node = relay.node()

    @strawberry.field
    def hello(self) -> str:
        return "hi"


nodes = strawberry.Schema(query=NodeQuery, mutation=NodeMutation)
MUTATION_NODE = base64.b64encode(b"Mutation:1").decode()


def _typenames(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = {value["__typename"]} if "__typename" in value else set()
        return names.union(*(_typenames(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_typenames(v) for v in value))
    return set()


HIDDEN = {"Mutation", "Subscription"}

CELLS = [
    (
        "Mutation: a field returning it",
        roots,
        GraphQLTool(),
        "{ payload { mutation { __typename } } }",
        HIDDEN,
    ),
    (
        "Mutation: a list of it",
        roots,
        GraphQLTool(),
        "{ payload { mutations { __typename } } }",
        HIDDEN,
    ),
    (
        "Mutation: an interface it implements",
        roots,
        GraphQLTool(),
        "{ maintenance { __typename reset } }",
        HIDDEN,
    ),
    (
        "Mutation: a list of an interface it implements",
        roots,
        GraphQLTool(),
        "{ resettables { __typename reset } }",
        HIDDEN,
    ),
    (
        "Mutation: an interface, from a nested type",
        roots,
        GraphQLTool(),
        "{ payload { resettable { __typename reset } } }",
        HIDDEN,
    ),
    (
        "Mutation: a union it is a member of",
        roots,
        GraphQLTool(),
        "{ anything { __typename } }",
        HIDDEN,
    ),
    (
        "Mutation: a list of a union, from a nested type",
        roots,
        GraphQLTool(),
        "{ payload { anything { __typename } } }",
        HIDDEN,
    ),
    (
        "Mutation: the Relay node field",
        nodes,
        GraphQLTool(),
        f'{{ node(id: "{MUTATION_NODE}") {{ __typename id }} }}',
        HIDDEN,
    ),
    (
        "Subscription: a field returning it",
        roots,
        GraphQLTool(),
        "{ payload { subscription { __typename } } }",
        HIDDEN,
    ),
    (
        "Subscription: an interface it implements",
        roots,
        GraphQLTool(),
        "{ ticker { __typename } }",
        HIDDEN,
    ),
    (
        "Query narrowed: an interface it implements",
        labelled,
        GraphQLTool(root_fields=["labelled", "hello"]),
        "{ labelled { __typename label } }",
        {"Query"},
    ),
    (
        "Mutation narrowed: an interface it implements",
        roots,
        GraphQLTool(allow_mutations=True, root_fields=["maintenance", "publish"]),
        "{ maintenance { __typename reset } }",
        {"Mutation"},
    ),
    (
        "Mutation narrowed by qualified names: an interface it implements",
        roots,
        GraphQLTool(
            allow_mutations=True,
            root_fields=["Query.maintenance", "Mutation.publish"],
        ),
        "{ maintenance { __typename reset } }",
        {"Mutation"},
    ),
]


@pytest.mark.parametrize(
    ("schema", "options", "query", "hidden"),
    [cell[1:] for cell in CELLS],
    ids=[cell[0] for cell in CELLS],
)
async def test_no_field_produces_a_value_of_a_type_the_tool_leaves_out(
    schema, options, query, hidden
):
    async with Client(mcp_server(schema, graphql_tool=options)) as client:
        result = await client.call_tool("graphql", {"query": query})

    data = (result.structured_content or {}).get("data")
    assert _typenames(data) & hidden == set()
    assert RAN == []


def _executing_on(schema):
    async def execute(query, variables, context, allowed):
        return await schema.execute(
            query,
            variable_values=variables,
            context_value=context,
            allowed_operation_types=allowed,
        )

    return execute


@pytest.mark.parametrize(
    ("schema", "options", "query", "hidden"),
    [cell[1:] for cell in CELLS],
    ids=[cell[0] for cell in CELLS],
)
async def test_nor_does_an_execute_hook_running_on_the_whole_schema(
    schema, options, query, hidden
):
    server = mcp_server(schema, graphql_tool=options, execute=_executing_on(schema))

    async with Client(server) as client:
        result = await client.call_tool("graphql", {"query": query})

    data = (result.structured_content or {}).get("data")
    assert _typenames(data) & hidden == set()
    assert RAN == []


@pytest.mark.parametrize(
    ("schema", "options", "query", "data"),
    [
        (roots, GraphQLTool(), "{ hello }", {"hello": "hi"}),
        (
            nodes,
            GraphQLTool(allow_mutations=True),
            f'{{ node(id: "{MUTATION_NODE}") {{ __typename id }} }}',
            {"node": {"__typename": "Mutation", "id": MUTATION_NODE}},
        ),
    ],
    ids=["a field that reaches none", "mutations allowed"],
)
async def test_an_execute_hook_runs_what_reaches_no_type_left_out(
    schema, options, query, data
):
    server = mcp_server(schema, graphql_tool=options, execute=_executing_on(schema))

    async with Client(server) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.structured_content == {"data": data}


async def test_an_execute_hook_is_told_which_field_may_reach_a_type_left_out():
    server = mcp_server(roots, graphql_tool=True, execute=_executing_on(roots))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": "{ payload { anything { __typename } } }"}
        )

    assert result.is_error is True
    assert (
        "Payload.anything may return a type this tool leaves out."
        in result.content[0].text
    )
    assert RAN == []


async def test_with_mutations_allowed_a_query_may_reach_one():
    server = mcp_server(roots, graphql_tool=GraphQLTool(allow_mutations=True))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": "{ maintenance { __typename reset } }"}
        )

    assert result.structured_content == {
        "data": {"maintenance": {"__typename": "Mutation", "reset": True}}
    }
    assert RAN == ["Mutation.reset"]


@pytest.mark.parametrize(
    ("schema", "options", "query", "data"),
    [
        (
            labelled,
            GraphQLTool(root_fields=["labelled", "hello"]),
            "{ hello }",
            {"hello": "hi"},
        ),
        (
            roots,
            GraphQLTool(allow_mutations=True, root_fields=["maintenance", "publish"]),
            "mutation { publish }",
            {"publish": True},
        ),
    ],
    ids=["Query", "Mutation"],
)
async def test_a_root_type_narrowed_out_of_an_interface_still_serves_its_fields(
    schema, options, query, data
):
    async with Client(mcp_server(schema, graphql_tool=options)) as client:
        result = await client.call_tool("graphql", {"query": query})
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert result.structured_content == {"data": data}
    assert " implements " not in sdl


OnlyRoots = Annotated[Mutation | Subscription, strawberry.union("OnlyRoots")]


@strawberry.type
class Admin:
    @strawberry.field
    def mutation(self) -> Mutation:
        return Mutation()


@strawberry.type(name="Query")
class AdminQuery:
    hello: str = "hi"

    @strawberry.field
    def admin(self) -> Admin | None:
        return Admin()

    @strawberry.field
    def either(self) -> OnlyRoots | None:
        return Mutation()


admin = strawberry.Schema(
    query=AdminQuery, mutation=Mutation, subscription=Subscription
)


async def test_a_type_that_reaches_only_types_left_out_is_left_out_too():
    async with Client(mcp_server(admin, graphql_tool=True)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        refused = [
            (await client.call_tool("graphql", {"query": query})).content[0].text
            for query in ("{ admin { __typename } }", "{ either { __typename } }")
        ]

    assert sdl.strip() == "type Query {\n  hello: String!\n}"
    assert "Cannot query field 'admin' on type 'Query'." in refused[0]
    assert "Cannot query field 'either' on type 'Query'." in refused[1]
    assert RAN == []


# root_fields, per root type

DELETED: list[int] = []


@strawberry.type
class PostMutations:
    @strawberry.field
    def delete_all(self) -> int:
        DELETED.append(1)
        return 0


@strawberry.type(name="Query")
class NamespacedQuery:
    @strawberry.field
    def posts(self) -> list[str]:
        return ["Hello"]


@strawberry.type(name="Mutation")
class NamespacedMutation:
    @strawberry.field
    def posts(self) -> PostMutations:
        return PostMutations()

    @strawberry.mutation
    def publish(self) -> bool:
        RAN.append("Mutation.publish")
        return True


namespaced = strawberry.Schema(query=NamespacedQuery, mutation=NamespacedMutation)


def test_a_name_on_both_roots_is_refused_when_mutations_are_allowed():
    options = GraphQLTool(allow_mutations=True, root_fields=["posts", "publish"])

    with pytest.raises(ValueError) as error:
        mcp_server(namespaced, graphql_tool=options)

    assert str(error.value) == (
        "root_fields names posts, a field of both Query and Mutation: "
        "write Query.posts or Mutation.posts."
    )


@pytest.mark.parametrize(
    "root_fields",
    [["Query.posts", "Mutation.publish"], ["Query.posts", "publish"]],
    ids=["qualified", "qualified and bare"],
)
async def test_qualified_names_keep_each_root_types_own_fields(root_fields):
    options = GraphQLTool(allow_mutations=True, root_fields=root_fields)
    DELETED.clear()

    async with Client(mcp_server(namespaced, graphql_tool=options)) as client:
        refused = await client.call_tool(
            "graphql", {"query": "mutation { posts { deleteAll } }"}
        )
        read = await client.call_tool("graphql", {"query": "{ posts }"})
        published = await client.call_tool("graphql", {"query": "mutation { publish }"})
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text

    assert refused.is_error is True
    assert "Cannot query field 'posts' on type 'Mutation'." in refused.content[0].text
    assert DELETED == []
    assert read.structured_content == {"data": {"posts": ["Hello"]}}
    assert published.structured_content == {"data": {"publish": True}}
    assert "deleteAll" not in sdl
    assert "PostMutations" not in sdl


async def test_a_name_may_be_qualified_on_both_roots():
    options = GraphQLTool(
        allow_mutations=True, root_fields=["Query.posts", "Mutation.posts"]
    )

    async with Client(mcp_server(namespaced, graphql_tool=options)) as client:
        result = await client.call_tool(
            "graphql", {"query": "mutation { posts { deleteAll } }"}
        )

    assert result.structured_content == {"data": {"posts": {"deleteAll": 0}}}


async def test_a_bare_name_on_both_roots_is_the_query_field_without_mutations():
    options = GraphQLTool(root_fields=["posts"])

    async with Client(mcp_server(namespaced, graphql_tool=options)) as client:
        result = await client.call_tool("graphql", {"query": "{ posts }"})

    assert result.structured_content == {"data": {"posts": ["Hello"]}}


@pytest.mark.parametrize(
    ("allow_mutations", "root_fields", "message"),
    [
        (
            False,
            ["Query.posts", "Mutation.publish"],
            "root_fields names no such root field: ['Mutation.publish'] "
            "(mutations need allow_mutations).",
        ),
        (
            True,
            ["Query.posts", "Subscription.posts"],
            "root_fields names no such root field: ['Subscription.posts'].",
        ),
        (
            True,
            ["Query.posts", "Query.publish"],
            "root_fields names no such root field: ['Query.publish'].",
        ),
        (
            True,
            ["Mutation.publish"],
            "root_fields must include at least one query field.",
        ),
    ],
    ids=["mutation without allow_mutations", "subscription", "wrong root", "no query"],
)
def test_qualified_names_are_checked(allow_mutations, root_fields, message):
    options = GraphQLTool(allow_mutations=allow_mutations, root_fields=root_fields)

    with pytest.raises(ValueError) as error:
        mcp_server(namespaced, graphql_tool=options)

    assert str(error.value) == message


async def test_names_may_be_qualified_by_the_root_types_own_names():
    @strawberry.type
    class Reads:
        @strawberry.field
        def posts(self) -> list[str]:
            return ["Hello"]

    @strawberry.type
    class Writes:
        @strawberry.mutation
        def publish(self) -> bool:
            return True

    schema = strawberry.Schema(query=Reads, mutation=Writes)
    options = GraphQLTool(
        allow_mutations=True, root_fields=["Reads.posts", "Writes.publish"]
    )

    async with Client(mcp_server(schema, graphql_tool=options)) as client:
        result = await client.call_tool("graphql", {"query": "mutation { publish }"})

    assert result.structured_content == {"data": {"publish": True}}


# A query that reaches a mutation is not run twice


@strawberry.type(name="Mutation")
class DjangoMutation(Resettable):
    @strawberry.mutation
    def reset(self) -> bool:
        RAN.append("Mutation.reset")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        raise SynchronousOnlyOperation("You cannot call this from an async context")


@strawberry.type(name="Query")
class DjangoQuery:
    @strawberry.field
    def maintenance(self) -> Resettable | None:
        return DjangoMutation()


django_like = strawberry.Schema(query=DjangoQuery, mutation=DjangoMutation)


async def test_a_query_that_reaches_a_mutation_is_not_rerun():
    server = mcp_server(django_like, graphql_tool=GraphQLTool(allow_mutations=True))

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": "{ maintenance { reset } }"}
        )

    assert RAN == ["Mutation.reset"]
    assert "a mutation is not run a second time" in result.content[0].text
