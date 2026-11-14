"""Grouping by related rows, date periods and ``@group_field`` (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from strawberry_orm import StrawberryORM
from tests.abstract.grouping_across_relations import (
    AbstractTestFilteringThroughAToManyRelation,
    AbstractTestGroupingAcrossRelations,
    AbstractTestGroupingByDateInterval,
)
from tests.backends.sqlalchemy.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


@pytest.fixture
def hide():
    return lambda model, name, query: query.where(model.name != name)


@pytest.fixture
def by_title():
    return lambda Post: select(Post).order_by(Post.title)


@pytest.fixture
def author_column():
    return lambda Post: Post.author_id


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _run


@pytest.fixture
def date_posts(sa_session, seed):
    async def _date_posts(dates):
        for pk, when in dates.items():
            sa_session.execute(
                update(Post).where(Post.id == pk).values(created_at=when)
            )
        sa_session.commit()

    return _date_posts


class TestGroupingAcrossRelations(AbstractTestGroupingAcrossRelations):
    pass


class TestGroupingByDateInterval(AbstractTestGroupingByDateInterval):
    pass


class TestFilteringThroughAToManyRelation(AbstractTestFilteringThroughAToManyRelation):
    pass
