"""Connection aggregates are computed from the stashed query, not the rows.

That query is built before the optimizer applies the type's ``scope_rows``,
so without explicit scoping a caller can read counts and sums over rows they
are not allowed to see, even though ``edges`` correctly hides them.
"""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


@pytest.mark.django_db
class TestConnectionAggregateScoping:
    def _build_schema(self, User):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(
            User,
            filters=orm.filter(User),
            order=orm.order(User),
            group=orm.group(User),
        )
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        return orm.schema(query=Query)

    QUERY = """
        query {
            users(first: 10) {
                totalCount
                edges { node { name } }
                aggregates { count }
            }
        }
    """

    def test_aggregate_count_matches_the_readable_rows(self, seed, User):
        result = self._build_schema(User).execute_sync(self.QUERY, context_value={})

        assert result.errors is None, result.errors
        users = result.data["users"]
        assert [edge["node"]["name"] for edge in users["edges"]] == ["Alice"]
        assert users["totalCount"] == 1
        assert users["aggregates"]["count"] == 1, (
            "aggregate counted rows the caller cannot read"
        )
