"""Regex patterns that backtrack for cubic time are refused (Django)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.regex_guard import AbstractTestRegexGuard
from tests.backends.django.models import User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_django(
            lazy_resolution="off", warn_missing_scope=False, **options
        )

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(User=User)


@pytest.fixture
def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


class TestRegexGuard(AbstractTestRegexGuard):
    pass
