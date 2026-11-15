"""Filter limits as a budget for the whole filter (Tortoise)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.filter_budget import AbstractTestFilterBudget
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**limits):
        return StrawberryORM.for_tortoise(
            lazy_resolution="off", warn_missing_scope=False, **limits
        )

    return _make_orm


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post)


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


@pytest.fixture
def statements(monkeypatch, tortoise_db):
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


class TestFilterBudget(AbstractTestFilterBudget):
    pass
