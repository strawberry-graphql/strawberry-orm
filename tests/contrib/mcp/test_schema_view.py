"""What the graphql tools tell a client about the schema: in suggestions, in
deprecations, and through fields that return a root type."""

from __future__ import annotations

import enum
from collections.abc import AsyncGenerator
from typing import NewType

import pytest
import strawberry
from mcp import Client
from strawberry.extensions import DisableIntrospection
from strawberry.schema.config import StrawberryConfig

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server

# Suggestions


@strawberry.enum
class Clearance(enum.Enum):
    PUBLIC = "public"
    TOP_SECRET = "ts"


@strawberry.type
class Doc:
    title: str
    internal_reviewer_notes: str = "secret notes"


@strawberry.input
class Match:
    title: str | None = None


@strawberry.type
class DocsQuery:
    @strawberry.field
    def documents(
        self, clearance: Clearance = Clearance.PUBLIC, match: Match | None = None
    ) -> list[Doc]:
        return [Doc(title="t")]


def _docs(*, introspection: bool = True, suggestions: bool = True):
    return strawberry.Schema(
        query=DocsQuery,
        extensions=[] if introspection else [DisableIntrospection],
        config=StrawberryConfig(disable_field_suggestions=not suggestions),
    )


MISTAKES = {
    "field": (
        "{ documents { internalReviewerNote } }",
        {},
        "Cannot query field 'internalReviewerNote' on type 'Doc'.",
    ),
    "argument": (
        "{ documents(clearence: PUBLIC) { title } }",
        {},
        "Unknown argument 'clearence' on field 'DocsQuery.documents'.",
    ),
    "enum value": (
        "{ documents(clearance: TOP_SECRE) { title } }",
        {},
        "Value 'TOP_SECRE' does not exist in 'Clearance' enum.",
    ),
    "enum value in a variable": (
        "query ($c: Clearance!) { documents(clearance: $c) { title } }",
        {"c": "TOP_SECRE"},
        "Variable '$c' has invalid value: "
        "Value 'TOP_SECRE' does not exist in 'Clearance' enum.",
    ),
    "input field in a variable": (
        "query ($m: Match) { documents(match: $m) { title } }",
        {"m": {"titel": "t"}},
        "Variable '$m' has invalid value: Expected value of type 'Match' "
        "not to include unknown field 'titel', found: {'titel': 't'}.",
    ),
}


async def _error(schema, options, query, variables=None):
    arguments = {"query": query, "variables": variables or {}}
    async with Client(mcp_server(schema, graphql_tool=options)) as client:
        result = await client.call_tool("graphql", arguments)
    assert result.is_error is True
    return result.content[0].text


@pytest.mark.parametrize(
    ("schema", "options"),
    [
        (
            _docs(introspection=False, suggestions=False),
            GraphQLTool(schema_tool=False),
        ),
        (_docs(suggestions=False), GraphQLTool()),
        (_docs(introspection=False), GraphQLTool(schema_tool=True)),
        (_docs(), GraphQLTool(schema_tool=False)),
    ],
    ids=[
        "no introspection or suggestions",
        "no suggestions",
        "no introspection",
        "no schema tool",
    ],
)
@pytest.mark.parametrize("mistake", list(MISTAKES), ids=list(MISTAKES))
async def test_the_graphql_tool_hides_suggestions_where_the_schema_is_kept_back(
    schema, options, mistake
):
    query, variables, message = MISTAKES[mistake]

    text = await _error(schema, options, query, variables)

    assert text.endswith(f"graphql: {message}")
    assert "Did you mean" not in text


async def test_the_graphql_tool_coerces_variables_that_fit():
    query, _, _ = MISTAKES["enum value in a variable"]

    async with Client(
        mcp_server(_docs(), graphql_tool=GraphQLTool(schema_tool=False))
    ) as client:
        result = await client.call_tool(
            "graphql", {"query": query, "variables": {"c": "TOP_SECRET"}}
        )

    assert result.structured_content == {"data": {"documents": [{"title": "t"}]}}


async def test_the_graphql_tool_suggests_where_the_schema_is_shared():
    query, _, _ = MISTAKES["field"]

    text = await _error(_docs(), GraphQLTool(), query)

    assert text.endswith("Did you mean 'internalReviewerNotes'?")


# Deprecations


@strawberry.enum
class Tier(enum.Enum):
    FREE = "free"
    GOLD = strawberry.enum_value("gold", deprecation_reason="Internal: GOLD migrates")


Sku = NewType("Sku", str)


@strawberry.type
class Item:
    name: str
    sku: Sku = "A-1"
    tier: Tier = Tier.FREE
    legacy_code: str = strawberry.field(
        default="x", deprecation_reason="Internal: read by the billing cron"
    )


@strawberry.type
class ItemsQuery:
    @strawberry.field
    def items(self) -> list[Item]:
        return [Item(name="a")]


items = strawberry.Schema(
    query=ItemsQuery,
    config=StrawberryConfig(
        scalar_map={
            Sku: strawberry.scalar(
                name="Sku", specified_by_url="https://example.com/sku"
            )
        }
    ),
)


async def _sdl(schema, options):
    async with Client(mcp_server(schema, graphql_tool=options)) as client:
        return (await client.call_tool("graphql_schema", {})).content[0].text


async def test_the_schema_without_descriptions_leaves_out_deprecation_reasons():
    sdl = await _sdl(items, GraphQLTool(descriptions=False))

    assert "legacyCode: String! @deprecated\n" in sdl
    assert "GOLD @deprecated\n" in sdl
    assert "Internal" not in sdl
    assert 'scalar Sku @specifiedBy(url: "https://example.com/sku")' in sdl


async def test_the_schema_with_descriptions_keeps_deprecation_reasons():
    sdl = await _sdl(items, GraphQLTool())

    assert '@deprecated(reason: "Internal: read by the billing cron")' in sdl
    assert '@deprecated(reason: "Internal: GOLD migrates")' in sdl


# Fields that return a root type


RESETS: list[str] = []


@strawberry.type
class Payload:
    ok: bool

    @strawberry.field
    def query(self) -> RelayQuery:
        return RelayQuery()

    @strawberry.field
    def mutation(self) -> RelayMutation:
        return RelayMutation()


@strawberry.type
class RelayQuery:
    @strawberry.field
    def payload(self) -> Payload:
        return Payload(ok=True)

    @strawberry.field
    def hidden(self) -> str:
        return "hidden root field"


@strawberry.type
class RelayMutation:
    @strawberry.mutation
    def reset(self) -> Payload:
        RESETS.append("reset")
        return Payload(ok=True)

    @strawberry.mutation
    def wipe(self) -> Payload:
        RESETS.append("wipe")
        return Payload(ok=True)


relay = strawberry.Schema(query=RelayQuery, mutation=RelayMutation)


async def _calls(options, *queries):
    async with Client(mcp_server(relay, graphql_tool=options)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        results = [
            await client.call_tool("graphql", {"query": query}) for query in queries
        ]
    return sdl, results


async def test_a_field_returning_the_query_type_returns_it_narrowed():
    sdl, (hidden, shown) = await _calls(
        GraphQLTool(root_fields=["payload"]),
        "{ payload { query { hidden } } }",
        "{ payload { query { payload { ok } } } }",
    )

    assert "type RelayQuery {\n  payload: Payload!\n}" in sdl
    assert "query: RelayQuery!" in sdl
    assert "hidden" not in sdl
    assert hidden.is_error is True
    assert "Cannot query field 'hidden' on type 'RelayQuery'." in hidden.content[0].text
    assert shown.structured_content == {
        "data": {"payload": {"query": {"payload": {"ok": True}}}}
    }


async def test_a_field_returning_a_mutation_type_left_out_is_left_out():
    RESETS.clear()

    sdl, (reached,) = await _calls(
        GraphQLTool(), "{ payload { mutation { reset { ok } } } }"
    )

    assert "RelayMutation" not in sdl
    assert "reset" not in sdl
    assert reached.is_error is True
    assert "Cannot query field 'mutation' on type 'Payload'." in reached.content[0].text
    assert RESETS == []


async def test_a_field_returning_the_mutation_type_returns_it_narrowed():
    RESETS.clear()

    sdl, (hidden, shown) = await _calls(
        GraphQLTool(allow_mutations=True, root_fields=["payload", "reset"]),
        "mutation { reset { mutation { wipe { ok } } } }",
        "mutation { reset { mutation { reset { ok } } } }",
    )

    assert "type RelayMutation {\n  reset: Payload!\n}" in sdl
    assert "wipe" not in sdl
    assert hidden.is_error is True
    assert (
        "Cannot query field 'wipe' on type 'RelayMutation'." in hidden.content[0].text
    )
    assert shown.structured_content == {
        "data": {"reset": {"mutation": {"reset": {"ok": True}}}}
    }
    assert RESETS == ["reset", "reset"]


async def test_root_fields_without_a_mutation_leave_out_fields_returning_it():
    sdl, _ = await _calls(
        GraphQLTool(allow_mutations=True, root_fields=["payload"]),
    )

    assert "RelayMutation" not in sdl
    assert "type Payload {\n  ok: Boolean!\n  query: RelayQuery!\n}" in sdl


@strawberry.type
class Ticks:
    @strawberry.subscription
    async def ticks(self) -> AsyncGenerator[int]:
        yield 1


async def test_the_subscription_type_is_never_shown():
    streaming = strawberry.Schema(query=RelayQuery, subscription=Ticks)

    async with Client(mcp_server(streaming, graphql_tool=True)) as client:
        sdl = (await client.call_tool("graphql_schema", {})).content[0].text
        refused = await client.call_tool("graphql", {"query": "subscription { ticks }"})

    assert "Ticks" not in sdl
    assert "ticks" not in sdl
    assert "Subscription operations are not allowed." in refused.content[0].text
