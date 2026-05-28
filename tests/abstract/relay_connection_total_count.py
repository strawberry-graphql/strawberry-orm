"""Shared tests for ``totalCount`` on ORM Relay connections."""

from collections.abc import Iterable

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

CONNECTION_TOTAL_COUNT_QUERY = """
{
  usersConnection(first: 1) {
    totalCount
    edges {
      node { name }
    }
    pageInfo { hasNextPage }
  }
}
"""

CONNECTION_FILTERED_TOTAL_COUNT_QUERY = """
{
  usersConnection(
    filter: { field: { email: { contains: "example.com" } } }
    first: 1
  ) {
    totalCount
    edges { node { name } }
    pageInfo { hasNextPage }
  }
}
"""


class AbstractTestRelayConnectionTotalCount:
    expected_total_users = 3
    expected_filtered_total_users = 2

    def _build_schema(self, orm, User, users_query):
        UserFilter = orm.filter(User)

        @orm.type(User, filters=UserFilter)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[UserNode])
            def users_connection(
                self, info: strawberry.types.Info
            ) -> Iterable[UserNode]:
                return users_query(User, info)  # type: ignore[return-value]

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_total_count_reflects_full_result_set(
        self, orm, seed, User, users_query, schema_execute
    ):
        """``totalCount`` is the size of the filtered queryset, not the page."""
        schema = self._build_schema(orm, User, users_query)
        result = schema_execute(schema, CONNECTION_TOTAL_COUNT_QUERY)
        assert result.errors is None
        conn = result.data["usersConnection"]
        assert conn["totalCount"] == self.expected_total_users
        assert len(conn["edges"]) == 1
        assert conn["pageInfo"]["hasNextPage"] is True

    def test_total_count_respects_filters(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self._build_schema(orm, User, users_query)
        result = schema_execute(schema, CONNECTION_FILTERED_TOTAL_COUNT_QUERY)
        assert result.errors is None
        conn = result.data["usersConnection"]
        assert conn["totalCount"] == self.expected_filtered_total_users
        assert len(conn["edges"]) == 1
        assert conn["pageInfo"]["hasNextPage"] is True

    def test_connection_works_without_total_count_selected(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self._build_schema(orm, User, users_query)
        result = schema_execute(
            schema,
            """
            {
              usersConnection(first: 2) {
                edges { node { name } }
              }
            }
            """,
        )
        assert result.errors is None
        assert len(result.data["usersConnection"]["edges"]) == 2


class AbstractTestRelayConnectionTotalCountAsync(AbstractTestRelayConnectionTotalCount):
    """Async backends (e.g. Tortoise) use ``await schema.execute``."""

    async def test_total_count_reflects_full_result_set(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self._build_schema(orm, User, users_query)
        result = await schema_execute(schema, CONNECTION_TOTAL_COUNT_QUERY)
        assert result.errors is None
        conn = result.data["usersConnection"]
        assert conn["totalCount"] == self.expected_total_users
        assert len(conn["edges"]) == 1
        assert conn["pageInfo"]["hasNextPage"] is True

    async def test_total_count_respects_filters(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self._build_schema(orm, User, users_query)
        result = await schema_execute(schema, CONNECTION_FILTERED_TOTAL_COUNT_QUERY)
        assert result.errors is None
        conn = result.data["usersConnection"]
        assert conn["totalCount"] == self.expected_filtered_total_users
        assert len(conn["edges"]) == 1
        assert conn["pageInfo"]["hasNextPage"] is True

    async def test_connection_works_without_total_count_selected(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self._build_schema(orm, User, users_query)
        result = await schema_execute(
            schema,
            """
            {
              usersConnection(first: 2) {
                edges { node { name } }
              }
            }
            """,
        )
        assert result.errors is None
        assert len(result.data["usersConnection"]["edges"]) == 2
