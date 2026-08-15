"""Filter project tests for SQLAlchemy — type generation + query tests."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.query_filter_object_project import (
    AbstractTestFilterProjectQueries,
    AbstractTestFilterProjectTypeGeneration,
)
from tests.backends.sqlalchemy.models import (
    Comment as SAComment,
)
from tests.backends.sqlalchemy.models import (
    Post as SAPost,
)
from tests.backends.sqlalchemy.models import (
    Tag as SATag,
)
from tests.backends.sqlalchemy.models import (
    User as SAUser,
)


class TestFilterProjectTypeGeneration(AbstractTestFilterProjectTypeGeneration):
    pass


def _build_projected_schema():
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
    orm.filter(SAUser)
    orm.filter(SATag)
    orm.filter(SAComment)
    PostFilter = orm.filter(SAPost, project={"author": {}})

    @orm.type(SATag)
    class TagType:
        id: auto
        name: auto

    @orm.type(SAComment)
    class CommentType:
        id: auto
        body: auto

    @orm.type(SAPost, filters=PostFilter)
    class PostType:
        id: auto
        title: auto
        body: auto
        is_published: auto
        tags: list[TagType]
        comments: list[CommentType]

    @orm.type(SAUser)
    class UserType:
        id: auto
        name: auto
        email: auto
        posts: list[PostType]

    @strawberry.type
    class Query:
        posts: list[PostType] = orm.field.auto()

    return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])


_projected_schema = _build_projected_schema()


@pytest.fixture
def execute_projected(sa_session, seed):
    def _execute(query, variables=None, expect_errors=False):
        result = _projected_schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": sa_session},
        )
        if expect_errors:
            return result
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


class TestFilterProjectQueries(AbstractTestFilterProjectQueries):
    pass
