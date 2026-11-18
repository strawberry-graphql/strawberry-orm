"""What one request's filters cost the database is bounded (Django)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.db.backends.sqlite3 import _functions

from strawberry_orm import StrawberryORM
from tests.abstract.fb_filter_cost import (
    AbstractTestCostlyLookupBudget,
    AbstractTestRegexAsAnalysed,
    AbstractTestRegexOnBoundedText,
)
from tests.backends.django.models import Post, User


@pytest.fixture
def make_orm():
    def _make_orm(**options):
        return StrawberryORM.for_django(
            lazy_resolution="off", warn_missing_scope=False, **options
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
def regex_filters():
    return True


@pytest.fixture
def regexp_calls(monkeypatch):
    """SQLite's ``REGEXP``, from whichever thread runs it, recording its calls."""

    @contextmanager
    def _regexp_calls():
        calls = []
        original = _functions.re_search

        def re_search(pattern, value):
            calls.append((pattern, value))
            return original(pattern, value)

        with monkeypatch.context() as patch:
            patch.setattr(_functions, "re_search", re_search)
            yield calls

    return _regexp_calls


class TestCostlyLookupBudget(AbstractTestCostlyLookupBudget):
    pass


class TestRegexOnBoundedText(AbstractTestRegexOnBoundedText):
    pass


class TestRegexAsAnalysed(AbstractTestRegexAsAnalysed):
    pass
