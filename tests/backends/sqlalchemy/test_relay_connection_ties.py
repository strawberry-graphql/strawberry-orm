"""Rows that tie on a connection's order land on exactly one page (SQLAlchemy).

Run on a ``Session`` and on an ``AsyncSession``.
"""

from contextlib import contextmanager

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.abstract.relay_connection_ties import (
    NAMES,
    TITLES,
    AbstractTestRelayConnectionTies,
    AbstractTestRelayConnectionTiesAsync,
)
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _tied_rows():
    users = [SAUser(name=name, email="tied@example.com") for name in NAMES]
    posts = [SAPost(title=title, body="tied", author=users[0]) for title in TITLES]
    return [*users, *posts, SAPost(title="Another", body="tied", author=users[1])]


@pytest.fixture
def by_email():
    return lambda User, info: select(User).order_by(User.email)


@pytest.fixture
def unordered():
    return lambda User, info: select(User)


@pytest.fixture
def by_default_order():
    pytest.skip("a SQLAlchemy model has no default order")


@pytest.fixture
def tied_authors():
    return lambda User, info: (
        select(User).join(User.posts).where(SAPost.body == "tied").distinct()
    )


@pytest.fixture
def first_twenty():
    return lambda query: query.where(SAUser.name < "User 21")


def _statements(session):
    bind = session.bind
    engine = getattr(bind, "sync_engine", bind)

    @contextmanager
    def _capture():
        sent: list[str] = []

        def before(conn, cursor, statement, params, context, executemany):
            sent.append(statement)

        event.listen(engine, "before_cursor_execute", before)
        try:
            yield sent
        finally:
            event.remove(engine, "before_cursor_execute", before)

    return _capture


class TestRelayConnectionTies(AbstractTestRelayConnectionTies):
    @pytest.fixture
    def tied(self, sa_session):
        sa_session.add_all(_tied_rows())
        sa_session.commit()

    @pytest.fixture
    def statements(self, sa_session):
        return _statements(sa_session)

    @pytest.fixture
    def execute(self, sa_session):
        return lambda schema, query: schema.execute_sync(
            query, context_value={"session": sa_session}
        )


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
class TestRelayConnectionTiesAsync(AbstractTestRelayConnectionTiesAsync):
    @pytest_asyncio.fixture
    async def tied(self, async_session):
        async_session.add_all(_tied_rows())
        await async_session.commit()

    @pytest.fixture
    def statements(self, async_session):
        return _statements(async_session)

    @pytest.fixture
    def execute(self, async_session):
        async def _execute(schema, query):
            return await schema.execute(query, context_value={"session": async_session})

        return _execute
