"""Filter project tests for Tortoise — type generation + async query tests."""

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.query_filter_object_project import (
    AbstractTestFilterProjectTypeGeneration,
)
from tests.backends.tortoise.models import (
    Comment as TortComment,
    Post as TortPost,
    Tag as TortTag,
    User as TortUser,
)


class TestFilterProjectTypeGeneration(AbstractTestFilterProjectTypeGeneration):
    pass


_orm = StrawberryORM("tortoise")
_orm.filter(TortUser)
_orm.filter(TortTag)
_orm.filter(TortComment)
_ProjectedPostFilter = _orm.filter(TortPost, project={"author": {}})


@_orm.type(TortTag)
class _TagType:
    id: auto
    name: auto


@_orm.type(TortComment)
class _CommentType:
    id: auto
    body: auto


@_orm.type(TortPost, filters=_ProjectedPostFilter)
class _PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    tags: list[_TagType]
    comments: list[_CommentType]


@_orm.type(TortUser)
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


@pytest_asyncio.fixture
async def execute_projected(seed):
    async def _execute(query, variables=None, expect_errors=False):
        result = await _projected_schema.execute(
            query, variable_values=variables or {}
        )
        if expect_errors:
            return result
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


class TestFilterProjectQueries:
    @pytest.mark.asyncio
    async def test_projected_filter_query(self, execute_projected, seed):
        data = await execute_projected("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Alice" } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    @pytest.mark.asyncio
    async def test_projected_filter_excludes_unprojected_relation(
        self, execute_projected, seed
    ):
        result = await execute_projected(
            """
            { posts(filter: {
                object: { tags: { field: { name: { exact: "python" } } } }
            }) { title } }
            """,
            expect_errors=True,
        )
        assert result is not None
