"""Rows that tie on a connection's order land on exactly one page (Django)."""

from contextlib import contextmanager

import pytest
from django.db.backends.sqlite3.base import SQLiteCursorWrapper

from tests.abstract.relay_connection_ties import (
    NAMES,
    TITLES,
    AbstractTestRelayConnectionTies,
    AbstractTestRelayConnectionTiesAsync,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


@pytest.fixture
def tied():
    users = [
        DjUser.objects.create(name=name, email="tied@example.com") for name in NAMES
    ]
    DjPost.objects.bulk_create(
        [DjPost(title=title, body="tied", author=users[0]) for title in TITLES]
        + [DjPost(title="Another", body="tied", author=users[1])]
    )


@pytest.fixture
def by_email():
    return lambda User, info: User.objects.order_by("email")


@pytest.fixture
def unordered():
    return lambda User, info: User.objects.all()


@pytest.fixture
def by_default_order(monkeypatch, unordered):
    monkeypatch.setattr(DjUser._meta, "ordering", ["email"])
    return unordered


@pytest.fixture
def tied_authors():
    return lambda User, info: User.objects.filter(posts__body="tied").distinct()


@pytest.fixture
def first_twenty():
    return lambda query: query.filter(name__lt="User 21")


@pytest.fixture
def statements(monkeypatch):
    """SQL from whichever thread runs it: async execution reads on a worker."""

    @contextmanager
    def _statements():
        sent: list[str] = []
        original = SQLiteCursorWrapper.execute

        def execute(self, query, params=None):
            sent.append(query)
            return original(self, query, params)

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteCursorWrapper, "execute", execute)
            yield sent

    return _statements


class TestRelayConnectionTies(AbstractTestRelayConnectionTies):
    @pytest.fixture
    def execute(self):
        return lambda schema, query: schema.execute_sync(query, context_value={})


@pytest.mark.asyncio
class TestRelayConnectionTiesAsync(AbstractTestRelayConnectionTiesAsync):
    @pytest.fixture
    def execute(self):
        async def _execute(schema, query):
            return await schema.execute(query, context_value={})

        return _execute
