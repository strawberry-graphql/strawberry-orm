"""Root connections cut their page in the database (SQLAlchemy).

Run on a ``Session`` and on an ``AsyncSession``: the count has to be awaited on
the second, and the two reads must not overlap on one session.
"""

from collections.abc import Iterable
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
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
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return select(User).order_by(User.id)

    return _users_query


@pytest.fixture
def published_authors():
    def _published_authors(User, Post, info):
        return (
            select(User)
            .join(User.posts)
            .where(Post.is_published.is_(True))
            .order_by(User.id)
            .distinct()
        )

    return _published_authors


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        return select(Post).order_by(Post.id)

    return _posts_query


def _extra_users():
    return [
        SAUser(id=i, name=f"User {i:02}", email=f"u{i}@example.com")
        for i in range(4, 13)
    ]


def _recorder(session):
    """Statements on *session*'s engine, and ``User`` rows built from them.

    The session is emptied first: a row already in its identity map is handed
    back without being loaded, which would hide how many rows were read.
    """
    bind = session.bind
    engine = getattr(bind, "sync_engine", bind)

    @contextmanager
    def _capture():
        seen = SimpleNamespace(statements=[], loaded=0)

        def before(conn, cursor, statement, params, context, executemany):
            seen.statements.append(statement)

        def loaded(target, context):
            seen.loaded += 1

        session.expunge_all()
        event.listen(engine, "before_cursor_execute", before)
        event.listen(SAUser, "load", loaded)
        try:
            yield seen
        finally:
            event.remove(SAUser, "load", loaded)
            event.remove(engine, "before_cursor_execute", before)

    return _capture


class TestRelayConnectionPagination(AbstractTestRelayConnectionPagination):
    @pytest.fixture
    def many_users(self, sa_session, seed):
        sa_session.add_all(_extra_users())
        sa_session.commit()
        return list(sa_session.scalars(select(SAUser.name).order_by(SAUser.id)))

    @pytest.fixture
    def capture(self, sa_session):
        return _recorder(sa_session)

    @pytest.fixture
    def execute(self, sa_session):
        def _execute(schema, query):
            return schema.execute_sync(query, context_value={"session": sa_session})

        return _execute


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SABase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
class TestRelayConnectionPaginationAsync(AbstractTestRelayConnectionPaginationAsync):
    @pytest_asyncio.fixture
    async def seed(self, async_session):
        alice = SAUser(id=1, name="Alice", email="alice@example.com")
        bob = SAUser(id=2, name="Bob", email="bob@example.com")
        charlie = SAUser(id=3, name="Charlie", email="charlie@test.org")
        async_session.add_all(
            [
                alice,
                bob,
                charlie,
                SAPost(
                    id=1, title="Hello World", body="b", is_published=True, author=alice
                ),
                SAPost(
                    id=2,
                    title="GraphQL Guide",
                    body="b",
                    is_published=True,
                    author=alice,
                ),
                SAPost(
                    id=3, title="Draft Post", body="b", is_published=False, author=bob
                ),
                SAPost(
                    id=4,
                    title="Rust Adventures",
                    body="b",
                    is_published=True,
                    author=charlie,
                ),
            ]
        )
        await async_session.commit()

    @pytest_asyncio.fixture
    async def many_users(self, async_session, seed):
        async_session.add_all(_extra_users())
        await async_session.commit()
        names = await async_session.scalars(select(SAUser.name).order_by(SAUser.id))
        return list(names)

    @pytest.fixture
    def capture(self, async_session):
        return _recorder(async_session)

    @pytest.fixture
    def execute(self, async_session):
        async def _execute(schema, query):
            return await schema.execute(query, context_value={"session": async_session})

        return _execute

    async def test_count_query_can_be_awaited(self, orm, async_session, seed):
        info = SimpleNamespace(context={"session": async_session})
        assert await orm._backend.count_query(select(SAUser), info) == 3


def _posts_by_author_schema(orm, query):
    @orm.type(SAPost)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @strawberry.type
    class Query:
        @orm.connection(ORMListConnection[PostNode])
        def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
            return query

    return orm.schema(query=Query)


class TestJoins:
    """``.unique()`` folds repeated rows together after they are read.

    Cutting a join that can repeat a row would count the repeats against the
    page, so such a join is still read whole. A join to one row per row is not.
    """

    def test_a_join_that_repeats_rows_pages_as_before(self, orm, sa_session, seed):
        # Alice has two published posts, so the join yields her twice.
        repeated = (
            select(SAUser)
            .join(SAUser.posts)
            .where(SAPost.is_published.is_(True))
            .order_by(SAUser.id)
        )
        schema = _users_schema(orm, SAUser, lambda User, info: repeated, None, SAPost)
        for shape in ["first: 1", f'first: 1, after: "{cursor(0)}"', "last: 1"]:
            query = page_query(shape)
            result = schema.execute_sync(query, context_value={"session": sa_session})
            assert outcome(result) == reference(query, ["Alice", "Charlie"]), shape

    def test_a_join_to_one_row_is_still_cut(self, orm, sa_session, seed, query_counter):
        by_author = select(SAPost).join(SAPost.author).order_by(SAUser.name, SAPost.id)
        result = _posts_by_author_schema(orm, by_author).execute_sync(
            "{ posts(first: 2) { edges { node { title } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert [e["node"]["title"] for e in result.data["posts"]["edges"]] == [
            "Hello World",
            "GraphQL Guide",
        ]
        assert len(query_counter) == 1
        assert "LIMIT" in query_counter[0].upper(), query_counter[0]
