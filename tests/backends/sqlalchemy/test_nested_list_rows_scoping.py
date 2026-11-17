from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.nested_list_rows_scoping import (
    AbstractTestNestedListRowsScoping,
    titles,
)
from tests.abstract.payload import policy
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


class TestNestedListRowsScoping(AbstractTestNestedListRowsScoping):
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
    suite = TestNestedListRowsScoping()
    suite.session = sa_session
    return suite


@pytest.mark.parametrize("optimizer", [True, False])
async def test_an_inner_async_iterable_is_scoped_like_a_list(
    suite, orm, models, optimizer
):
    PostType = suite.post_type(orm, models)
    posts = await suite.load_posts()

    async def stream(rows):
        for row in rows:
            yield row

    @strawberry.type
    class Query:
        @strawberry.field
        def pages(self, info: strawberry.Info) -> list[list[PostType]]:
            return [stream(posts[:2]), stream(posts[2:])]

        @strawberry.field
        def streamed(self, info: strawberry.Info) -> list[list[PostType]]:
            return stream([posts[2:], posts[:2]])

    data = await suite.execute(
        orm.schema(query=Query, optimizer=optimizer),
        "{ pages { title author { name } } streamed { title } }",
    )
    assert titles(data["pages"]) == [
        ["Hello World", "GraphQL Guide"],
        ["Rust Adventures"],
    ]
    assert titles(data["streamed"]) == [
        ["Rust Adventures"],
        ["Hello World", "GraphQL Guide"],
    ]
