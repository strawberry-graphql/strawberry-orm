"""AsyncSession-specific tests for the SQLAlchemy backend."""

from __future__ import annotations

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.fixtures import main_schema
from tests.backends.sqlalchemy.models import (
    Base as SABase,
    Post as SAPost,
    Tag as SATag,
    User as SAUser,
)


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SABase.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def async_seed(async_session):
    alice = SAUser(id=1, name="Alice", email="alice@example.com")
    bob = SAUser(id=2, name="Bob", email="bob@example.com")
    charlie = SAUser(id=3, name="Charlie", email="charlie@test.org")

    python = SATag(id=1, name="python")
    graphql = SATag(id=2, name="graphql")
    rust = SATag(id=3, name="rust")

    p1 = SAPost(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = SAPost(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    p3 = SAPost(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = SAPost(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )

    p1.tags.append(python)
    p2.tags.extend([python, graphql])
    p4.tags.append(rust)

    async_session.add_all([alice, bob, charlie, python, graphql, rust, p1, p2, p3, p4])
    await async_session.commit()
    return async_session


class TestAsyncSessionExecution:
    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_async_session(self, async_seed):
        result = await main_schema.execute(
            "{ users { name posts { title } } }",
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    @pytest.mark.asyncio
    async def test_async_session_supports_relation_ordering(self, async_seed):
        result = await main_schema.execute(
            """
            {
                users {
                    name
                    posts(order: [{ title: DESC }]) {
                        title
                    }
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }


class TestAsyncSessionMutations:
    @pytest.mark.asyncio
    async def test_apply_ref_list_can_be_awaited_with_async_session(self, async_seed):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        TagRef = orm.ref(SATag)

        @strawberry.type
        class Query:
            @strawberry.field
            def ping(self) -> str:
                return "pong"

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query, mutation=Mutation)
        result = await schema.execute(
            """
            mutation {
                setPostTags(postId: 3, tags: [{ id: "1" }, { id: "3" }]) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert sorted(result.data["setPostTags"], key=lambda tag: tag["name"]) == [
            {"name": "python"},
            {"name": "rust"},
        ]
