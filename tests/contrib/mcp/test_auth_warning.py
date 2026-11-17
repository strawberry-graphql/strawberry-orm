"""A server whose tools can change data warns when it is built without
authentication."""

from __future__ import annotations

import warnings

import pytest
import strawberry
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server

from .fakes import Query, schema

UNAUTHENTICATED = "can change data, and it has no authentication"


class _Verifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        return None


AUTH = {
    "token_verifier": _Verifier(),
    "auth": AuthSettings(
        issuer_url="https://auth.example.com",
        resource_server_url="https://mcp.example.com/mcp",
        validate_token_resource=True,
    ),
}


@pytest.mark.parametrize(
    "tools",
    [
        {"operations": 'mutation Publish { publish(title: "x") { title } }'},
        {"graphql_tool": GraphQLTool(allow_mutations=True)},
    ],
    ids=["a mutation tool", "graphql with mutations"],
)
def test_a_server_that_can_change_data_warns_without_authentication(tools):
    with pytest.warns(UserWarning, match=UNAUTHENTICATED) as caught:
        mcp_server(schema, **tools)

    [warning] = caught
    assert warning.filename == __file__
    assert "token_verifier=" in str(warning.message)


@pytest.mark.parametrize(
    ("server_schema", "tools"),
    [
        (schema, {"operations": "query Named { named { name } }"}),
        (schema, {"graphql_tool": True}),
        (schema, {"graphql_tool": GraphQLTool(allow_mutations=True), **AUTH}),
        (
            schema,
            {"operations": 'mutation P { publish(title: "x") { title } }', **AUTH},
        ),
        (
            schema,
            {"graphql_tool": GraphQLTool(allow_mutations=True, root_fields=["named"])},
        ),
        (
            strawberry.Schema(query=Query),
            {"graphql_tool": GraphQLTool(allow_mutations=True)},
        ),
    ],
    ids=[
        "query tools",
        "graphql for queries",
        "graphql with mutations, authenticated",
        "a mutation tool, authenticated",
        "root fields without a mutation",
        "a schema without mutations",
    ],
)
def test_a_server_that_cannot_change_data_or_authenticates_does_not_warn(
    server_schema, tools
):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mcp_server(server_schema, **tools)

    assert [str(w.message) for w in caught if UNAUTHENTICATED in str(w.message)] == []
