"""Root connections cut their page in the database (Django)."""

from collections.abc import Iterable
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import strawberry
from django.db.backends.sqlite3.base import SQLiteCursorWrapper
from django.db.models.signals import post_init
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.relay_connection_pagination import (
    AbstractTestRelayConnectionPagination,
    AbstractTestRelayConnectionPaginationAsync,
    _users_schema,
    cursor,
    outcome,
    page_query,
    reference,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return User.objects.order_by("id")

    return _users_query


@pytest.fixture
def published_authors():
    def _published_authors(User, Post, info):
        return User.objects.filter(posts__is_published=True).order_by("id").distinct()

    return _published_authors


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        # Unordered: Django adds a queryset's ordering to GROUP BY, which would
        # split every group into one row each. SQLite returns them by id.
        return Post.objects.all()

    return _posts_query


@pytest.fixture
def many_users(seed):
    for i in range(4, 13):
        DjUser.objects.create(id=i, name=f"User {i:02}", email=f"u{i}@example.com")
    return list(DjUser.objects.order_by("id").values_list("name", flat=True))


@pytest.fixture
def capture(monkeypatch):
    """Statements and ``User`` rows, from whichever thread runs the query.

    Async execution reads on a worker thread with its own connection, which
    ``CaptureQueriesContext`` on the test thread would not see.
    """

    @contextmanager
    def _capture():
        seen = SimpleNamespace(statements=[], loaded=0)
        original = SQLiteCursorWrapper.execute

        def execute(self, query, params=None):
            seen.statements.append(query)
            return original(self, query, params)

        def count(sender, instance, **kwargs):
            seen.loaded += 1

        monkeypatch.setattr(SQLiteCursorWrapper, "execute", execute)
        post_init.connect(count, sender=DjUser, weak=False)
        try:
            yield seen
        finally:
            post_init.disconnect(count, sender=DjUser)
            monkeypatch.setattr(SQLiteCursorWrapper, "execute", original)

    return _capture


class TestRelayConnectionPagination(AbstractTestRelayConnectionPagination):
    @pytest.fixture
    def execute(self):
        def _execute(schema, query):
            return schema.execute_sync(query, context_value={})

        return _execute


@pytest.mark.asyncio
class TestRelayConnectionPaginationAsync(AbstractTestRelayConnectionPaginationAsync):
    @pytest.fixture
    def execute(self):
        async def _execute(schema, query):
            return await schema.execute(query, context_value={})

        return _execute

    async def test_count_query_can_be_awaited(self, orm, seed):
        assert await orm._backend.count_query(DjUser.objects.all(), None) == 3


class TestJoins:
    """Django keeps a join's repeated rows, read whole or cut, and counts them."""

    def test_a_join_that_repeats_rows_pages_as_before(self, orm, seed):
        # Alice has two published posts, so the join yields her twice.
        repeated = DjUser.objects.filter(posts__is_published=True).order_by("id")
        schema = _users_schema(orm, DjUser, lambda User, info: repeated, None, DjPost)
        for shape in ["first: 1", f'first: 1, after: "{cursor(0)}"', "last: 1"]:
            query = page_query(shape)
            result = schema.execute_sync(query, context_value={})
            assert outcome(result) == reference(query, ["Alice", "Alice", "Charlie"]), (
                shape
            )

    def test_a_join_to_one_row_is_still_cut(self, orm, seed, capture):
        @orm.type(DjPost)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return DjPost.objects.order_by("author__name", "id")

        with capture() as seen:
            result = orm.schema(query=Query).execute_sync(
                "{ posts(first: 2) { edges { node { title } } } }"
            )
        assert result.errors is None, result.errors
        assert [e["node"]["title"] for e in result.data["posts"]["edges"]] == [
            "Hello World",
            "GraphQL Guide",
        ]
        assert len(seen.statements) == 1
        assert "LIMIT" in seen.statements[0].upper(), seen.statements[0]
