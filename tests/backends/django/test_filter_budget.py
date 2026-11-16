"""Filter limits as a budget for the whole filter (Django)."""

from types import SimpleNamespace

import pytest

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


class TestFilterBudget(AbstractTestFilterBudget):
    pass
