"""Filter limits as a budget for the whole filter (SQLAlchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.filter_budget import AbstractTestFilterBudget
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**limits):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **limits
        )

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


class TestFilterBudget(AbstractTestFilterBudget):
    pass
