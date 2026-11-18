from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.db.backends.sqlite3.base import SQLiteCursorWrapper

from strawberry_orm import StrawberryORM
from tests.abstract.filter_value_size import AbstractTestFilterValueSize
from tests.backends.django.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_django(lazy_resolution="off", **options)

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
def bound(monkeypatch):
    @contextmanager
    def _bound():
        sizes: list[int] = []
        original = SQLiteCursorWrapper.execute

        def execute(self, query, params=None):
            sizes.append(sum(len(v) for v in params or () if isinstance(v, str)))
            return original(self, query, params)

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteCursorWrapper, "execute", execute)
            yield sizes

    return _bound


class TestFilterValueSize(AbstractTestFilterValueSize):
    pass
