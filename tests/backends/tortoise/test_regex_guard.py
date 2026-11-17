"""Regex patterns that backtrack for cubic time are refused (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.regex_guard import AbstractTestRegexGuard
from tests.backends.tortoise.models import User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_tortoise(
            lazy_resolution="off", warn_missing_scope=False, **options
        )

    return _make_orm


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User)


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestRegexGuard(AbstractTestRegexGuard):
    pass
