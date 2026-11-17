"""Regex patterns that backtrack for cubic time are refused (SQLAlchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.regex_guard import AbstractTestRegexGuard
from tests.backends.sqlalchemy.models import User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **options
        )

    return _make_orm


@pytest.fixture
def models():
    return SimpleNamespace(User=User)


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


class TestRegexGuard(AbstractTestRegexGuard):
    pass
