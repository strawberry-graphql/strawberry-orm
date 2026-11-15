from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.resolver_rows_scoping import (
    PUBLISHED_TITLES,
    AbstractTestResolverRowsScoping,
)
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestResolverRowsScoping(AbstractTestResolverRowsScoping):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return await schema.execute(query, context_value={"session": self.session})

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    async def load_posts(self):
        return list(self.session.scalars(select(Post).order_by(Post.id)))


@pytest.fixture
def suite(sa_session, seed):
    suite = TestResolverRowsScoping()
    suite.session = sa_session
    return suite


@pytest.mark.parametrize("optimizer", [True, False])
async def test_a_scalar_result_is_scoped_like_a_list(suite, orm, models, optimizer):
    PostType = suite.post_type(orm, models)

    @strawberry.type
    class Query:
        @strawberry.field
        def posts(self, info: strawberry.Info) -> list[PostType]:
            return info.context["session"].scalars(select(Post).order_by(Post.id))

    data = await suite.execute(
        orm.schema(query=Query, optimizer=optimizer), "{ posts { title } }"
    )
    assert [post["title"] for post in data["posts"]] == PUBLISHED_TITLES


@pytest.mark.parametrize("optimizer", [True, False])
async def test_an_async_iterable_of_rows_is_scoped_like_a_list(
    suite, orm, models, optimizer
):
    PostType = suite.post_type(orm, models)
    posts = await suite.load_posts()

    async def stream():
        for post in posts:
            yield post

    @strawberry.type
    class Query:
        @strawberry.field
        def posts(self, info: strawberry.Info) -> list[PostType]:
            return stream()

        @strawberry.field
        async def awaited(self, info: strawberry.Info) -> list[PostType]:
            return stream()

    schema = orm.schema(query=Query, optimizer=optimizer)
    data = await suite.execute(
        schema, "{ posts { title author { name } } awaited { title } }"
    )
    assert [post["title"] for post in data["posts"]] == PUBLISHED_TITLES
    assert [post["title"] for post in data["awaited"]] == PUBLISHED_TITLES
