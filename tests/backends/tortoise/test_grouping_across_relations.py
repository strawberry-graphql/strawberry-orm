"""Grouping by related rows, date periods and ``@group_field`` (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise.expressions import F

from strawberry_orm import StrawberryORM
from tests.abstract.grouping_across_relations import (
    AbstractTestAggregateSelections,
    AbstractTestARelationToManyRowsCanBeEmpty,
    AbstractTestFilteringThroughAToManyRelation,
    AbstractTestGroupingAcrossRelations,
    AbstractTestGroupingByDateInterval,
    AbstractTestOrderingByASetOfRelatedRows,
    AbstractTestTheClientsOrderReplacesTheStatements,
    AbstractTestTheRowsTogetherOnEachDatabase,
    together_path,
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


@pytest_asyncio.fixture
async def add_posts(seed):
    async def _add_posts(posts):
        for pk, (title, author_id, tags) in posts.items():
            post = await Post.create(
                id=pk, title=title, body="", is_published=True, author_id=author_id
            )
            await post.tags.add(*await Tag.filter(name__in=tags))

    return _add_posts


@pytest_asyncio.fixture
async def add_tag(seed):
    async def _add_tag(post_id, name):
        post = await Post.get(id=post_id)
        await post.tags.add(await Tag.get(name=name))

    return _add_tag


class TestGroupingAcrossRelations(AbstractTestGroupingAcrossRelations):
    pass


class TestGroupingByDateInterval(AbstractTestGroupingByDateInterval):
    pass


class TestFilteringThroughAToManyRelation(AbstractTestFilteringThroughAToManyRelation):
    pass


class TestAggregateSelections(AbstractTestAggregateSelections):
    pass


class TestARelationToManyRowsCanBeEmpty(AbstractTestARelationToManyRowsCanBeEmpty):
    pass


class TestTheClientsOrderReplacesTheStatements(
    AbstractTestTheClientsOrderReplacesTheStatements
):
    pass


class TestOrderingByASetOfRelatedRows(AbstractTestOrderingByASetOfRelatedRows):
    pass


@pytest.fixture
def together_sql(orm, models, monkeypatch):
    from strawberry_orm.backends.tortoise import _GroupedRows

    async def _sql(database):
        query = Post.all()
        query._choose_db_if_not_chosen()
        monkeypatch.setattr(
            query._db, "capabilities", SimpleNamespace(dialect=database)
        )
        path = together_path(orm.backend, Post)
        rows = _GroupedRows(orm.backend, query, Post, [path], None)
        return rows.stmt.select(*rows.selected()).get_sql()

    return _sql


@pytest.fixture
def together_read(orm, models):
    return lambda column, interval: orm.backend.together_reader(Post, column, interval)


class TestTheRowsTogetherOnEachDatabase(AbstractTestTheRowsTogetherOnEachDatabase):
    pass
