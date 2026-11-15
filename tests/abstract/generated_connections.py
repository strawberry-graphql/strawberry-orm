"""Connections the library resolves itself, executed without a ``context_value``.

A generated connection builds its query from the ``filter`` and ``order``
arguments, and its ``totalCount``, ``aggregates`` and ``groups`` read that query
again later in the request. Both are easy to get wrong quietly: applying the
inputs twice still returns the right rows, and a request with no context used
to drop the stashed query and answer ``null``.

A backend provides ``orm`` (able to execute without a context), ``User`` and
the seed, plus ``schema_execute(schema, query)`` executing with no
``context_value``.
"""

from types import SimpleNamespace

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.relay.connection import connection_states
from strawberry_orm.types import auto

FILTERED_QUERY = """
{
  users(
    first: 10
    filter: { field: { email: { contains: "example.com" } } }
    order: [{ field: { name: DESC } }]
  ) {
    totalCount
    edges { node { name } }
  }
}
"""

GROUPED_QUERY = """
{
  users(first: 10, groupBy: [{ field: { name: true } }]) {
    totalCount
    aggregates { count }
    groups { key { name } aggregates { count } }
  }
}
"""


class AbstractTestGeneratedConnections:
    def schema(self, orm, User):
        @orm.type(
            User,
            filters=orm.filter(User),
            order=orm.order(User),
            group=orm.group(User),
        )
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        return orm.schema(query=Query)

    def count_calls(self, monkeypatch, orm, name):
        calls = []
        original = getattr(orm._backend, name)

        def counted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(orm._backend, name, counted)
        return calls

    def _assert_filtered_once(self, result, filters, orders):
        assert result.errors is None, result.errors
        users = result.data["users"]
        assert [edge["node"]["name"] for edge in users["edges"]] == ["Bob", "Alice"]
        assert users["totalCount"] == 2
        assert len(filters) == 1, filters
        assert len(orders) == 1, orders

    def _assert_grouped(self, result):
        assert result.errors is None, result.errors
        users = result.data["users"]
        assert users["totalCount"] == 3
        assert users["aggregates"] == {"count": 3}
        assert {
            g["key"]["name"]: g["aggregates"]["count"] for g in users["groups"]
        } == {
            "Alice": 1,
            "Bob": 1,
            "Charlie": 1,
        }
        # The fallback store lives for one execution and no longer.
        assert connection_states(SimpleNamespace(context=None)) is None

    def test_filter_and_order_are_applied_once(
        self, orm, seed, User, schema_execute, monkeypatch
    ):
        schema = self.schema(orm, User)
        filters = self.count_calls(monkeypatch, orm, "apply_filters")
        orders = self.count_calls(monkeypatch, orm, "apply_ordering")
        result = schema_execute(schema, FILTERED_QUERY)
        self._assert_filtered_once(result, filters, orders)

    def test_groups_and_aggregates_need_no_context(
        self, orm, seed, User, schema_execute
    ):
        self._assert_grouped(schema_execute(self.schema(orm, User), GROUPED_QUERY))


class AbstractTestGeneratedConnectionsAsync(AbstractTestGeneratedConnections):
    async def test_filter_and_order_are_applied_once(
        self, orm, seed, User, schema_execute, monkeypatch
    ):
        schema = self.schema(orm, User)
        filters = self.count_calls(monkeypatch, orm, "apply_filters")
        orders = self.count_calls(monkeypatch, orm, "apply_ordering")
        result = await schema_execute(schema, FILTERED_QUERY)
        self._assert_filtered_once(result, filters, orders)

    async def test_groups_and_aggregates_need_no_context(
        self, orm, seed, User, schema_execute
    ):
        self._assert_grouped(
            await schema_execute(self.schema(orm, User), GROUPED_QUERY)
        )
