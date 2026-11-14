"""Grouping by related rows, date periods and ``@group_field`` (Django)."""

from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db.models import F

from strawberry_orm import StrawberryORM
from tests.abstract.grouping_across_relations import (
    AbstractTestAggregateSelections,
    AbstractTestARelationToManyRowsCanBeEmpty,
    AbstractTestFilteringThroughAToManyRelation,
    AbstractTestGroupingAcrossRelations,
    AbstractTestGroupingByDateInterval,
    AbstractTestTheClientsOrderReplacesTheStatements,
    AbstractTestTheRowsTogetherOnEachDatabase,
    together_path,
)
from tests.backends.django.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


@pytest.fixture
def hide():
    return lambda model, name, query: query.exclude(name=name)


@pytest.fixture
def by_title():
    return lambda Post: Post.objects.order_by("title")


@pytest.fixture
def author_column():
    return lambda Post: F("author_id")


@pytest.fixture
def run(seed):
    async def _run(schema, query):
        return await schema.execute(query, context_value={})

    return _run


@pytest.fixture
def date_posts(seed):
    @sync_to_async
    def _date_posts(dates):
        for pk, when in dates.items():
            Post.objects.filter(pk=pk).update(created_at=when)

    return _date_posts


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


@pytest.fixture
def together_sql(orm, monkeypatch):
    from django.db import connections

    async def _sql(database):
        vendor = {"postgres": "postgresql"}.get(database, database)
        monkeypatch.setattr(type(connections["default"]), "vendor", vendor)
        path = together_path(orm.backend, Post)
        return str(orm.backend._grouped_rows(Post.objects.all(), [path], None).query)

    return _sql


@pytest.fixture
def together_read(orm):
    return lambda column, interval: orm.backend.together_reader(Post, column, interval)


class TestTheRowsTogetherOnEachDatabase(AbstractTestTheRowsTogetherOnEachDatabase):
    pass
