"""AsyncSession-specific tests for the SQLAlchemy backend."""

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.fixtures import main_schema
from tests.backends.sqlalchemy.models import (
    Base as SABase,
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
                    posts(order: [{ field: { title: DESC } }]) {
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
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
                setPostTags(postId: 3, tags: [{ update: { id: "1" } }, { update: { id: "3" } }]) {
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

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_supports_create_update_and_delete(
        self, async_seed
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class CreateTagInput:
            name: str

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType]

        TagRef = orm.ref(
            SATag,
            create=CreateTagInput,
            update=UpdateTagInput,
            unlink=True,
            delete=True,
        )

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field()
            posts: list[PostType] = orm.field()

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def modify_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query,
            mutation=Mutation,
            extensions=[orm.optimizer_extension()],
        )

        result = await schema.execute(
            """
            mutation {
                modifyPostTags(
                    postId: 2
                    tags: [
                        { update: { id: "1", name: "python-async" } }
                        { create: { name: "fresh-async" } }
                        { delete: { id: "2" } }
                    ]
                ) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert sorted(result.data["modifyPostTags"], key=lambda tag: tag["name"]) == [
            {"name": "fresh-async"},
            {"name": "python-async"},
        ]

        query_result = await schema.execute(
            """
            {
                tags {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert query_result.errors is None
        all_tag_names = sorted(tag["name"] for tag in query_result.data["tags"])
        assert "graphql" not in all_tag_names
        assert "fresh-async" in all_tag_names
        assert "python-async" in all_tag_names

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_supports_delete_and_create(self, async_seed):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class CreateTagInput:
            name: str

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType]

        TagRef = orm.ref(SATag, create=CreateTagInput, delete=True)

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field()
            tags: list[TagType] = orm.field()

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def modify_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query,
            mutation=Mutation,
            extensions=[orm.optimizer_extension()],
        )
        result = await schema.execute(
            """
            mutation {
                modifyPostTags(
                    postId: 1
                    tags: [
                        { delete: { id: "1" } }
                        { create: { name: "patched-async" } }
                    ]
                ) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data["modifyPostTags"] == [{"name": "patched-async"}]

        query_result = await schema.execute(
            """
            {
                tags {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert query_result.errors is None
        all_tag_names = sorted(tag["name"] for tag in query_result.data["tags"])
        assert "python" not in all_tag_names
        assert "patched-async" in all_tag_names
