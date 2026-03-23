"""Filter project tests for Django — type generation + query tests."""

import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.query_filter_object_project import (
    AbstractTestFilterProjectQueries,
    AbstractTestFilterProjectTypeGeneration,
)
from tests.backends.django.models import (
    Comment as DjComment,
)
from tests.backends.django.models import (
    Post as DjPost,
)
from tests.backends.django.models import (
    Tag as DjTag,
)
from tests.backends.django.models import (
    User as DjUser,
)


class TestFilterProjectTypeGeneration(AbstractTestFilterProjectTypeGeneration):
    pass


_orm = StrawberryORM("django")
_orm.filter(DjUser)
_orm.filter(DjTag)
_orm.filter(DjComment)
_ProjectedPostFilter = _orm.filter(DjPost, project={"author": {}})


@_orm.type(DjTag)
class _TagType:
    id: auto
    name: auto


@_orm.type(DjComment)
class _CommentType:
    id: auto
    body: auto


@_orm.type(DjPost, filters=_ProjectedPostFilter)
class _PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    tags: list[_TagType]
    comments: list[_CommentType]


@_orm.type(DjUser)
class _UserType:
    id: auto
    name: auto
    email: auto
    posts: list[_PostType]


@strawberry.type
class _Query:
    posts: list[_PostType] = _orm.field()


_projected_schema = strawberry.Schema(
    query=_Query,
    extensions=[_orm.optimizer_extension()],
)


def _execute(query, variables=None, expect_errors=False):
    result = _projected_schema.execute_sync(query, variable_values=variables or {})
    if expect_errors:
        return result
    assert result.errors is None, f"GraphQL errors: {result.errors}"
    return result.data


import pytest  # noqa: E402


@pytest.fixture
def execute_projected(seed):
    return _execute


class TestFilterProjectQueries(AbstractTestFilterProjectQueries):
    pass
