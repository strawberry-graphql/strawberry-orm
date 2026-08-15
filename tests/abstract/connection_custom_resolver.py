"""Connections where the caller supplies the queryset.

``orm.connection(resolver=...)`` (and the decorator form) hands row selection
to the caller while the library keeps building everything around it: the
``filter``/``order``/``groupBy`` arguments, the grouped connection type,
``totalCount``, and optimizer integration. These tests pin that down, because
the easy failure mode is a supplied resolver quietly getting a plainer field
than the generated one.
"""

from collections.abc import Iterable

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class AbstractTestConnectionCustomResolver:
    all_names = ["Alice", "Bob", "Charlie"]
    example_com_names = ["Alice", "Bob"]

    def _node(self, orm, User, *, group=False):
        kwargs = {"filters": orm.filter(User), "order": orm.order(User)}
        if group:
            kwargs["group"] = orm.group(User)

        @orm.type(User, **kwargs)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        return UserNode

    def keyword_schema(self, orm, User, users_query):
        """The resolver passed by keyword, with the field assigned."""
        UserNode = self._node(orm, User)

        def resolve_users(info: strawberry.types.Info) -> Iterable[UserNode]:
            return users_query(User, info)

        @strawberry.type
        class Query:
            users = orm.connection(ORMListConnection[UserNode], resolver=resolve_users)

        return orm.schema(query=Query)

    def decorator_grouped_schema(self, orm, User, users_query):
        """The resolver as a decorator on a node that declares a group-by."""
        UserNode = self._node(orm, User, group=True)

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[UserNode])
            def users(self, info: strawberry.types.Info) -> Iterable[UserNode]:
                return users_query(User, info)

        return orm.schema(query=Query)

    def argument_schema(self, orm, User, users_query, narrow):
        """A resolver with an argument of its own, alongside the generated ones."""
        UserNode = self._node(orm, User)

        def resolve_users(
            info: strawberry.types.Info, name: str = ""
        ) -> Iterable[UserNode]:
            query = users_query(User, info)
            return narrow(query, name) if name else query

        @strawberry.type
        class Query:
            users = orm.connection(ORMListConnection[UserNode], resolver=resolve_users)

        return orm.schema(query=Query)

    # -- assertions shared by the sync and async variants --------------------

    def _assert_returns_all_rows(self, result):
        assert result.errors is None, result.errors
        users = result.data["users"]
        names = [edge["node"]["name"] for edge in users["edges"]]
        assert sorted(names) == self.all_names
        assert users["totalCount"] == len(self.all_names)

    def _assert_filter_applied(self, result):
        assert result.errors is None, result.errors
        users = result.data["users"]
        names = [edge["node"]["name"] for edge in users["edges"]]
        assert sorted(names) == self.example_com_names
        assert users["totalCount"] == len(self.example_com_names)

    def _assert_order_applied(self, result):
        assert result.errors is None, result.errors
        names = [edge["node"]["name"] for edge in result.data["users"]["edges"]]
        assert names == sorted(self.all_names, reverse=True)

    def _assert_groups(self, result):
        assert result.errors is None, result.errors
        users = result.data["users"]
        assert users["aggregates"]["count"] == len(self.all_names)
        counts = {
            group["key"]["name"]: group["aggregates"]["count"]
            for group in users["groups"]
        }
        assert counts == dict.fromkeys(self.all_names, 1)

    def _assert_own_argument(self, result):
        assert result.errors is None, result.errors
        assert [edge["node"]["name"] for edge in result.data["users"]["edges"]] == [
            "Alice"
        ]

    ALL_QUERY = """
        { users(first: 10) { totalCount edges { node { name } } } }
    """
    FILTER_QUERY = """
        {
          users(
            first: 10
            filter: { field: { email: { contains: "example.com" } } }
          ) {
            totalCount
            edges { node { name } }
          }
        }
    """
    ORDER_QUERY = """
        {
          users(first: 10, order: [{ field: { name: DESC } }]) {
            edges { node { name } }
          }
        }
    """
    GROUP_QUERY = """
        {
          users(first: 10, groupBy: [{ field: { name: true } }]) {
            aggregates { count }
            groups { key { name } aggregates { count } }
          }
        }
    """
    ARGUMENT_QUERY = """
        { users(first: 10, name: "Alice") { edges { node { name } } } }
    """

    # -- tests ---------------------------------------------------------------

    def test_keyword_resolver_supplies_the_rows(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_returns_all_rows(schema_execute(schema, self.ALL_QUERY))

    def test_generated_filter_argument_applies_to_a_supplied_resolver(
        self, orm, seed, User, users_query, schema_execute
    ):
        """The resolver never sees ``filter``; the library still applies it."""
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_filter_applied(schema_execute(schema, self.FILTER_QUERY))

    def test_generated_order_argument_applies_to_a_supplied_resolver(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_order_applied(schema_execute(schema, self.ORDER_QUERY))

    def test_supplied_resolver_still_gets_the_grouped_connection_type(
        self, orm, seed, User, users_query, schema_execute
    ):
        """A group-by on the node builds the grouped type for supplied resolvers."""
        schema = self.decorator_grouped_schema(orm, User, users_query)
        assert "groupBy" in str(schema)
        self._assert_groups(schema_execute(schema, self.GROUP_QUERY))

    def test_resolver_keeps_its_own_arguments(
        self, orm, seed, User, users_query, narrow_by_name, schema_execute
    ):
        schema = self.argument_schema(orm, User, users_query, narrow_by_name)
        self._assert_own_argument(schema_execute(schema, self.ARGUMENT_QUERY))


class AbstractTestConnectionCustomResolverAsync(AbstractTestConnectionCustomResolver):
    """Async backends (e.g. Tortoise) await execution."""

    async def test_keyword_resolver_supplies_the_rows(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_returns_all_rows(await schema_execute(schema, self.ALL_QUERY))

    async def test_generated_filter_argument_applies_to_a_supplied_resolver(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_filter_applied(await schema_execute(schema, self.FILTER_QUERY))

    async def test_generated_order_argument_applies_to_a_supplied_resolver(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.keyword_schema(orm, User, users_query)
        self._assert_order_applied(await schema_execute(schema, self.ORDER_QUERY))

    async def test_supplied_resolver_still_gets_the_grouped_connection_type(
        self, orm, seed, User, users_query, schema_execute
    ):
        schema = self.decorator_grouped_schema(orm, User, users_query)
        assert "groupBy" in str(schema)
        self._assert_groups(await schema_execute(schema, self.GROUP_QUERY))

    async def test_resolver_keeps_its_own_arguments(
        self, orm, seed, User, users_query, narrow_by_name, schema_execute
    ):
        schema = self.argument_schema(orm, User, users_query, narrow_by_name)
        self._assert_own_argument(await schema_execute(schema, self.ARGUMENT_QUERY))
