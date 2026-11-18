from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.filter_value_size import AbstractTestFilterValueSize
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_tortoise(lazy_resolution="off", **options)

    return _make_orm


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


@pytest.fixture
def bound(monkeypatch, tortoise_db):
    from tortoise import connections

    @contextmanager
    def _bound():
        sizes: list[int] = []
        client = connections.get("default")

        def recording(original):
            async def run(sql, values=None, *args, **kwargs):
                sizes.append(sum(len(v) for v in values or () if isinstance(v, str)))
                return await original(sql, values, *args, **kwargs)

            return run

        with monkeypatch.context() as patch:
            for name in ("execute_query", "execute_query_dict"):
                patch.setattr(client, name, recording(getattr(client, name)))
            yield sizes

    return _bound


class TestFilterValueSize(AbstractTestFilterValueSize):
    pass
