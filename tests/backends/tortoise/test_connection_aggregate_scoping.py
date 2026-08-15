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
from tests.backends.tortoise.models import User as TortoiseUser


class TestConnectionAggregateScoping:
    def _build_schema(self):
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
        )

        @orm.type(
            TortoiseUser,
            filters=orm.filter(TortoiseUser),
            order=orm.order(TortoiseUser),
            group=orm.group(TortoiseUser),
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

    @pytest.mark.asyncio
    async def test_aggregate_count_matches_the_readable_rows(self, seed):
        result = await self._build_schema().execute(self.QUERY, context_value={})

        assert result.errors is None, result.errors
        users = result.data["users"]
        assert [edge["node"]["name"] for edge in users["edges"]] == ["Alice"]
        assert users["totalCount"] == 1
        assert users["aggregates"]["count"] == 1, (
            "aggregate counted rows the caller cannot read"
        )
