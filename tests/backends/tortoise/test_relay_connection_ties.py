"""Rows that tie on a connection's order land on exactly one page (Tortoise)."""

from contextlib import contextmanager

import pytest
import pytest_asyncio
from pypika_tortoise import Order

from tests.abstract.relay_connection_ties import (
    NAMES,
    TITLES,
    AbstractTestRelayConnectionTiesAsync,
)
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


@pytest_asyncio.fixture
async def tied(tortoise_db):
    users = [await TUser.create(name=name, email="tied@example.com") for name in NAMES]
    await TPost.bulk_create(
        [TPost(title=title, body="tied", author=users[0]) for title in TITLES]
        + [TPost(title="Another", body="tied", author=users[1])]
    )


@pytest.fixture
def by_email():
    return lambda User, info: User.all().order_by("email")


@pytest.fixture
def unordered():
    return lambda User, info: User.all()


@pytest.fixture
def by_default_order(monkeypatch, unordered):
    monkeypatch.setattr(TUser._meta, "_default_ordering", (("email", Order.asc),))
    return unordered


@pytest.fixture
def tied_authors():
    return lambda User, info: User.filter(posts__body="tied").distinct()


@pytest.fixture
def first_twenty():
    return lambda query: query.filter(name__lt="User 21")


@pytest.fixture
def statements(monkeypatch):
    from tortoise import connections

    @contextmanager
    def _statements():
        sent: list[str] = []
        client = connections.get("default")

        def recording(original):
            async def run(sql, *args, **kwargs):
                sent.append(sql)
                return await original(sql, *args, **kwargs)

            return run

        with monkeypatch.context() as patch:
            for name in ("execute_query", "execute_query_dict"):
                patch.setattr(client, name, recording(getattr(client, name)))
            yield sent

    return _statements


@pytest.fixture
def execute():
    async def _execute(schema, query):
        return await schema.execute(query, context_value={})

    return _execute


@pytest.mark.asyncio
class TestRelayConnectionTies(AbstractTestRelayConnectionTiesAsync):
    pass
