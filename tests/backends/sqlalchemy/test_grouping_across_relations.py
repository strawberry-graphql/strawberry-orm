"""Grouping by related rows, date periods and ``@group_field`` (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

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


class TestAggregateSelections(AbstractTestAggregateSelections):
    pass


class TestARelationToManyRowsCanBeEmpty(AbstractTestARelationToManyRowsCanBeEmpty):
    pass


class TestTheClientsOrderReplacesTheStatements(
    AbstractTestTheClientsOrderReplacesTheStatements
):
    pass


@pytest.fixture
def together_sql():
    from sqlalchemy.dialects import postgresql

    async def _sql(database):
        dialect = {"postgres": "postgresql"}.get(database, database)
        backend = StrawberryORM.for_sqlalchemy(
            dialect=dialect, warn_missing_scope=False
        ).backend
        path = together_path(backend, Post)
        stmt, _ = backend._grouped_select(select(Post), Post, [path], None)
        return str(stmt.compile(dialect=postgresql.dialect()))

    return _sql


@pytest.fixture
def together_read(orm):
    return lambda column, interval: orm.backend.together_reader(Post, column, interval)


class TestTheRowsTogetherOnEachDatabase(AbstractTestTheRowsTogetherOnEachDatabase):
    pass
