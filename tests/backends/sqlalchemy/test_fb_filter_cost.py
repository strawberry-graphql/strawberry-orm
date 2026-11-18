"""What one request's filters cost the database is bounded (SQLAlchemy)."""

import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.fb_filter_cost import (
    AbstractTestCostlyLookupBudget,
    AbstractTestRegexAsAnalysed,
    AbstractTestRegexOnBoundedText,
)
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **options
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


@pytest.fixture
def regex_filters():
    return True


@pytest.fixture
def regexp_calls(sa_session):
    """The session's connection, with a ``regexp`` that records its calls."""

    @contextmanager
    def _regexp_calls():
        calls = []

        def regexp(pattern, value):
            calls.append((pattern, value))
            return None if value is None else re.search(pattern, value) is not None

        connection = sa_session.connection().connection.dbapi_connection
        connection.create_function("regexp", 2, regexp)
        yield calls

    return _regexp_calls


class TestCostlyLookupBudget(AbstractTestCostlyLookupBudget):
    pass


class TestRegexOnBoundedText(AbstractTestRegexOnBoundedText):
    pass


class TestRegexAsAnalysed(AbstractTestRegexAsAnalysed):
    pass
