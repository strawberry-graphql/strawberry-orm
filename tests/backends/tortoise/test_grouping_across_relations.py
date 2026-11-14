"""Grouping by related rows, date periods and ``@group_field`` (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise.expressions import F

from strawberry_orm import StrawberryORM
from tests.abstract.grouping_across_relations import (
    AbstractTestFilteringThroughAToManyRelation,
    AbstractTestGroupingAcrossRelations,
    AbstractTestGroupingByDateInterval,
)
from tests.backends.tortoise.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


@pytest.fixture
def hide():
    return lambda model, name, query: query.exclude(name=name)


@pytest.fixture
def by_title():
    return lambda Post: Post.all().order_by("title")


@pytest.fixture
def author_column():
    return lambda Post: F("author_id")


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query):
        return await schema.execute(query, context_value={})

    return _run


@pytest_asyncio.fixture
async def date_posts(seed):
    async def _date_posts(dates):
        for pk, when in dates.items():
            await Post.filter(id=pk).update(created_at=when)

    return _date_posts


class TestGroupingAcrossRelations(AbstractTestGroupingAcrossRelations):
    pass


class TestGroupingByDateInterval(AbstractTestGroupingByDateInterval):
    pass


class TestFilteringThroughAToManyRelation(AbstractTestFilteringThroughAToManyRelation):
    pass
