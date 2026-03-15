"""All fixtures for Tortoise backend tests."""

from typing import Optional

import pytest
import pytest_asyncio
import strawberry
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.tortoise.models import (
    Comment as TortComment,
    Post as TortPost,
    Tag as TortTag,
    User as TortUser,
)

_main_orm = StrawberryORM("tortoise")

UserFilter = _main_orm.filter(TortUser)
UserOrder = _main_orm.order(TortUser)
PostFilter = _main_orm.filter(TortPost)
PostOrder = _main_orm.order(TortPost)
CommentFilter = _main_orm.filter(TortComment)
CommentOrder = _main_orm.order(TortComment)
TagFilter = _main_orm.filter(TortTag)
TagOrder = _main_orm.order(TortTag)


@_main_orm.type(TortComment, filters=CommentFilter, order=CommentOrder)
class CommentType:
    id: auto
    body: auto
    post_id: int
    author_id: int
    parent_id: Optional[int]


@_main_orm.type(TortTag, filters=TagFilter, order=TagOrder)
class TagType:
    id: auto
    name: auto


@_main_orm.type(TortPost, filters=PostFilter, order=PostOrder)
class PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    tags: list[TagType]
    comments: list[CommentType]


@_main_orm.type(TortUser, filters=UserFilter, order=UserOrder)
class UserType:
    id: auto
    name: auto
    email: auto
    posts: list[PostType]


@strawberry.input
class CreateTagInput:
    name: str


@strawberry.input
class UpdateTagInput:
    id: strawberry.ID
    name: str


@strawberry.input
class CreatePostInput:
    title: str
    body: str
    is_published: bool = False
    author_id: int = 1


@strawberry.input
class UpdatePostInput:
    id: int
    title: Optional[str] = strawberry.UNSET
    body: Optional[str] = strawberry.UNSET
    is_published: Optional[bool] = strawberry.UNSET


TagRef = _main_orm.ref(
    TortTag,
    create=CreateTagInput,
    update=UpdateTagInput,
    delete=True,
)


@strawberry.type
class _MainQuery:
    users: list[UserType] = _main_orm.field()
    posts: list[PostType] = _main_orm.field()
    comments: list[CommentType] = _main_orm.field()

    @strawberry.field
    async def user(self, id: int) -> Optional[UserType]:
        return await TortUser.get_or_none(pk=id)  # type: ignore[return-value]


@strawberry.type
class _MainMutation:
    @strawberry.mutation
    async def create_post(self, input: CreatePostInput) -> PostType:
        post = await TortPost.create(
            title=input.title,
            body=input.body,
            is_published=input.is_published,
            author_id=input.author_id,
        )
        return post  # type: ignore[return-value]

    @strawberry.mutation
    async def update_post(self, input: UpdatePostInput) -> Optional[PostType]:
        post = await TortPost.get_or_none(pk=input.id)
        if post is None:
            return None
        if input.title is not strawberry.UNSET and input.title is not None:
            post.title = input.title
        if input.body is not strawberry.UNSET and input.body is not None:
            post.body = input.body
        if (
            input.is_published is not strawberry.UNSET
            and input.is_published is not None
        ):
            post.is_published = input.is_published
        await post.save()
        return post  # type: ignore[return-value]

    @strawberry.mutation
    async def delete_post(self, id: int) -> bool:
        deleted = await TortPost.filter(pk=id).delete()
        return deleted > 0

    @strawberry.mutation
    async def set_post_tags(
        self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
    ) -> Optional[PostType]:
        post = await TortPost.get_or_none(pk=post_id)
        if post is None:
            return None
        await _main_orm.apply_ref_list(post, "tags", tags, info)
        return post  # type: ignore[return-value]


main_schema = strawberry.Schema(
    query=_MainQuery,
    mutation=_MainMutation,
    extensions=[_main_orm.optimizer_extension()],
)


@pytest_asyncio.fixture
async def tortoise_db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["tests.backends.tortoise.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


# -- Fresh ORM instance -----------------------------------------------------


@pytest.fixture
def orm():
    return StrawberryORM("tortoise")


# -- Model class fixtures ----------------------------------------------------


@pytest.fixture
def User():
    return TortUser


@pytest.fixture
def Post():
    return TortPost


@pytest.fixture
def Tag():
    return TortTag


@pytest.fixture
def Comment():
    return TortComment


@pytest_asyncio.fixture
async def seed(tortoise_db):
    alice = await TortUser.create(id=1, name="Alice", email="alice@example.com")
    bob = await TortUser.create(id=2, name="Bob", email="bob@example.com")
    charlie = await TortUser.create(id=3, name="Charlie", email="charlie@test.org")

    python = await TortTag.create(id=1, name="python")
    graphql = await TortTag.create(id=2, name="graphql")
    rust = await TortTag.create(id=3, name="rust")

    p1 = await TortPost.create(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = await TortPost.create(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    p3 = await TortPost.create(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = await TortPost.create(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )

    await p1.tags.add(python)
    await p2.tags.add(python, graphql)
    await p4.tags.add(rust)

    c1 = await TortComment.create(id=1, body="Nice post!", post=p1, author=bob)
    c2 = await TortComment.create(
        id=2, body="Thanks!", post=p1, author=alice, parent_id=1
    )
    c3 = await TortComment.create(id=3, body="Great guide", post=p2, author=charlie)

    return {
        "users": {"alice": alice, "bob": bob, "charlie": charlie},
        "tags": {"python": python, "graphql": graphql, "rust": rust},
        "posts": {
            "hello_world": p1,
            "graphql_guide": p2,
            "draft": p3,
            "rust_adventures": p4,
        },
        "comments": {"nice_post": c1, "thanks": c2, "great_guide": c3},
    }


def _make_executor(target_schema):
    async def _execute(query, variables=None):
        result = await target_schema.execute(
            query,
            variable_values=variables or {},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


@pytest_asyncio.fixture
async def execute(seed):
    return _make_executor(main_schema)
