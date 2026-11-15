"""Filter limits as a budget for the whole filter (Django)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.db.backends.sqlite3.base import SQLiteCursorWrapper

from strawberry_orm import StrawberryORM
from tests.abstract.filter_budget import AbstractTestFilterBudget
from tests.backends.django.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**limits):
        return StrawberryORM.for_django(
            lazy_resolution="off", warn_missing_scope=False, **limits
        )

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


@pytest.fixture
def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


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


class TestFilterBudget(AbstractTestFilterBudget):
    pass
