"""The graphql tool shows no more of the schema than graphql_schema does.

Its operations are checked against the narrowed schema but run on the whole
one, where ``__schema`` and ``__type`` would describe root fields
``root_fields`` leaves out, the mutation type, and types no allowed field
reaches. The tool refuses them and points to ``graphql_schema`` instead.
"""

from __future__ import annotations

import pytest
from mcp import Client

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server

from .fakes import schema

REFUSAL = "Introspection is not available here; call graphql_schema for the schema."

#: Names only the whole schema has, which must not reach the client.
HIDDEN = ("missing", "publish", "Mutation", "secret", "Post")

INTROSPECTION = {
    "schema": "{ __schema { queryType { fields { name } } "
    "mutationType { fields { name } } types { name } } }",
    "type": '{ __type(name: "Post") { fields { name } } }',
    "fragment": "query { ...Root } "
    "fragment Root on Query { __schema { types { name } } }",
    "inline fragment": '{ ... on Query { __type(name: "Mutation") { name } } }',
    "beside a field": "{ named { name } __schema { types { name } } }",
}


async def _call(options, query):
    server = mcp_server(schema, graphql_tool=options)
    async with Client(server) as client:
        return await client.call_tool("graphql", {"query": query})


@pytest.mark.parametrize("query", list(INTROSPECTION.values()), ids=list(INTROSPECTION))
async def test_introspection_under_root_fields_is_refused(query):
    result = await _call(GraphQLTool(root_fields=["named"]), query)

    assert result.is_error is True
    text = result.content[0].text
    assert REFUSAL in text
    for name in HIDDEN:
        assert name not in text, text


async def test_introspection_cannot_find_mutations_that_are_not_allowed():
    result = await _call(True, "{ __schema { mutationType { name fields { name } } } }")

    assert result.is_error is True
    assert REFUSAL in result.content[0].text
    assert "publish" not in result.content[0].text


async def test_typename_still_answers():
    result = await _call(
        GraphQLTool(root_fields=["named"]), "{ __typename named { __typename name } }"
    )

    assert result.structured_content == {
        "data": {
            "__typename": "Query",
            "named": {"__typename": "Author", "name": "Alice"},
        }
    }
