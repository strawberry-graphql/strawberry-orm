"""Root connections cut their page in the database (Tortoise)."""

from collections.abc import Iterable
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.relay_connection_pagination import (
    AbstractTestRelayConnectionPaginationAsync,
    _users_schema,
    cursor,
    outcome,
    page_query,
    reference,
)
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return User.all().order_by("id")

    return _users_query


@pytest.fixture
def published_authors():
    def _published_authors(User, Post, info):
        return User.filter(posts__is_published=True).order_by("id").distinct()

    return _published_authors


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        return Post.all().order_by("id")

    return _posts_query


@pytest_asyncio.fixture
async def many_users(seed):
    for i in range(4, 13):
        await TUser.create(id=i, name=f"User {i:02}", email=f"u{i}@example.com")
    return list(await TUser.all().order_by("id").values_list("name", flat=True))


@pytest.fixture
def capture(monkeypatch):
    """Statements sent through the client, and ``User`` rows built from them."""
    from tortoise import connections

    @contextmanager
    def _capture():
        seen = SimpleNamespace(statements=[], loaded=0)
        client = connections.get("default")

        def recording(original):
            async def run(sql, *args, **kwargs):
                seen.statements.append(sql)
                return await original(sql, *args, **kwargs)

            return run

        build = TUser._init_from_db.__func__

        def counted(cls, **row):
            seen.loaded += 1
            return build(cls, **row)

        with monkeypatch.context() as patch:
            for name in ("execute_query", "execute_query_dict"):
                patch.setattr(client, name, recording(getattr(client, name)))
            patch.setattr(TUser, "_init_from_db", classmethod(counted))
            yield seen

    return _capture


@pytest.fixture
def execute():
    async def _execute(schema, query):
        return await schema.execute(query, context_value={})

    return _execute


@pytest.mark.asyncio
class TestRelayConnectionPagination(AbstractTestRelayConnectionPaginationAsync):
    async def test_count_query_counts_distinct_rows(self, orm, seed):
        """Tortoise's own count drops DISTINCT and counts the join's repeats."""
        distinct = TUser.filter(posts__is_published=True).distinct()
        assert len(await distinct) == 2
        assert await orm._backend.count_query(distinct, None) == 2


@pytest.mark.asyncio
class TestJoins:
    """A cut page holds the same rows, repeats included, as reading them all."""

    async def test_a_join_that_repeats_rows_pages_as_before(self, orm, seed, execute):
        repeated = TUser.filter(posts__is_published=True).order_by("id")
        read_whole = [user.name for user in await repeated]
        schema = _users_schema(orm, TUser, lambda User, info: repeated, None, TPost)
        for shape in ["first: 1", f'first: 1, after: "{cursor(0)}"', "last: 1"]:
            query = page_query(shape)
            result = await execute(schema, query)
            assert outcome(result) == reference(query, read_whole), shape

    async def test_a_join_to_one_row_is_still_cut(self, orm, seed, execute, capture):
        @orm.type(TPost)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return TPost.all().order_by("author__name", "id")

        with capture() as seen:
            result = await execute(
                orm.schema(query=Query),
                "{ posts(first: 2) { edges { node { title } } } }",
            )
        assert result.errors is None, result.errors
        assert [e["node"]["title"] for e in result.data["posts"]["edges"]] == [
            "Hello World",
            "GraphQL Guide",
        ]
        assert len(seen.statements) == 1
        assert "LIMIT" in seen.statements[0].upper(), seen.statements[0]
