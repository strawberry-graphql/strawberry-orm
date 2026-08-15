"""Connection aggregates are computed from the stashed query, not the rows.

That query is built before the optimizer applies the type's ``scope_rows``,
so without explicit scoping a caller can read counts over rows they are not
allowed to see, even though ``edges`` correctly hides them.
"""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import User as SAUser


class TestConnectionAggregateScoping:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._sa_session = sa_session

    def _build_schema(self):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
        )

        @orm.type(
            SAUser,
            filters=orm.filter(SAUser),
            order=orm.order(SAUser),
            group=orm.group(SAUser),
        )
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(SAUser.name == "Alice")

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

    def test_aggregate_count_matches_the_readable_rows(self):
        result = self._build_schema().execute_sync(
            self.QUERY, context_value={"session": self._sa_session}
        )

        assert result.errors is None, result.errors
        users = result.data["users"]
        assert [edge["node"]["name"] for edge in users["edges"]] == ["Alice"]
        assert users["totalCount"] == 1
        assert users["aggregates"]["count"] == 1, (
            "aggregate counted rows the caller cannot read"
        )
