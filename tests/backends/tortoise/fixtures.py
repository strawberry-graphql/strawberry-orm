"""All fixtures for Tortoise backend tests."""

from typing import Optional

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay
from strawberry.types.cast import cast as strawberry_cast
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
    name: str | None = strawberry.UNSET


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
    unlink=True,
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


def _build_node_mutation_schema():
    node_orm = StrawberryORM("tortoise")
    node_project = {
        "post": {
            "author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
            "comments": {
                "author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
            },
            "tags": {},
        },
        "comment": {
            "author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
        },
    }

    @node_orm.type(TortUser)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        email: auto

    @node_orm.type(TortTag)
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @node_orm.type(TortComment)
    class CommentNode(relay.Node):
        id: relay.NodeID[int]
        body: auto

        @strawberry.field
        async def author(self) -> UserNode:
            await self.fetch_related("author")
            return strawberry_cast(UserNode, self.author)

    @node_orm.type(TortPost)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        body: auto
        is_published: auto

        @strawberry.field
        async def author(self) -> UserNode:
            await self.fetch_related("author")
            return strawberry_cast(UserNode, self.author)

        @strawberry.field
        async def tags(self) -> list[TagNode]:
            return [strawberry_cast(TagNode, tag) for tag in await self.tags.all()]

        @strawberry.field
        async def comments(self) -> list[CommentNode]:
            return [
                strawberry_cast(CommentNode, comment)
                for comment in await self.comments.all().order_by("id")
            ]

    @strawberry.type(name="Query")
    class NodeQuery:
        @strawberry.field
        async def users(self) -> list[UserNode]:
            return [
                strawberry_cast(UserNode, user)
                for user in await TortUser.all().order_by("id")
            ]

        @strawberry.field
        async def posts(self) -> list[PostNode]:
            return [
                strawberry_cast(PostNode, post)
                for post in await TortPost.all().order_by("id")
            ]

        @strawberry.field
        async def comments(self) -> list[CommentNode]:
            return [
                strawberry_cast(CommentNode, comment)
                for comment in await TortComment.all().order_by("id")
            ]

        @strawberry.field
        async def tags(self) -> list[TagNode]:
            return [
                strawberry_cast(TagNode, tag)
                for tag in await TortTag.all().order_by("id")
            ]

    def _selected_root_key(input_obj: object) -> str:
        for field_name in input_obj.__class__.__dataclass_fields__:
            if getattr(input_obj, field_name) is not strawberry.UNSET:
                return field_name
        raise ValueError("Exactly one root model must be selected")

    CreateNodeInput = node_orm.mutations.create_node_input(name="CreateNodeInput")
    UpdateNodeInput = node_orm.mutations.update_node_input(name="UpdateNodeInput")

    @strawberry.type(name="Mutation")
    class NodeMutation:
        create_node = node_orm.mutations.create_node(input_name="CreateNodeInput")
        update_node = node_orm.mutations.update_node(input_name="UpdateNodeInput")
        projected_create_node = node_orm.mutations.create_node(
            project=node_project,
            input_name="ProjectedCreateNodeInput",
        )
        projected_update_node = node_orm.mutations.update_node(
            project=node_project,
            input_name="ProjectedUpdateNodeInput",
        )

        @strawberry.field
        async def inspect_create_node_input(self, input: CreateNodeInput) -> str:
            return _selected_root_key(input)

        @strawberry.field
        async def inspect_update_node_input(self, input: UpdateNodeInput) -> str:
            return _selected_root_key(input)

    return strawberry.Schema(
        query=NodeQuery,
        mutation=NodeMutation,
        extensions=[node_orm.optimizer_extension()],
    )


# =========================================================================
# Self-is-model schema
# =========================================================================

_self_orm = StrawberryORM("tortoise")


@_self_orm.type(TortPost)
class PostWithSummary:
    id: auto
    title: auto
    body: auto

    @strawberry.field
    def summary(self) -> str:
        return f"{self.title}: {self.body[:10]}"

    @strawberry.field
    def title_upper(self) -> str:
        return self.title.upper()


@_self_orm.type(TortUser)
class UserWithCustom:
    id: auto
    name: auto
    email: auto
    posts: list[PostWithSummary]

    @strawberry.field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"

    @strawberry.field
    async def post_count(self) -> int:
        return len(await self.posts.all())


@strawberry.type
class _SelfModelQuery:
    @strawberry.field
    async def users(self) -> list[UserWithCustom]:
        return await TortUser.all().order_by("id")  # type: ignore[return-value]

    @strawberry.field
    async def posts(self) -> list[PostWithSummary]:
        return await TortPost.all().order_by("id")  # type: ignore[return-value]


self_model_schema = strawberry.Schema(
    query=_SelfModelQuery,
    extensions=[_self_orm.optimizer_extension()],
)


# =========================================================================
# Get-queryset schema
# =========================================================================

_qs_orm = StrawberryORM("tortoise")


@_qs_orm.type(TortPost)
class PublishedPostType:
    id: auto
    title: auto
    is_published: auto

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)


@_qs_orm.type(TortUser)
class UnscopedUserType:
    id: auto
    name: auto


@strawberry.type
class _GetQuerysetQuery:
    @strawberry.field
    def posts(self) -> list[PublishedPostType]:
        return TortPost.all()  # type: ignore[return-value]

    @strawberry.field
    def users(self) -> list[UnscopedUserType]:
        return TortUser.all()  # type: ignore[return-value]


get_queryset_schema = strawberry.Schema(
    query=_GetQuerysetQuery,
    extensions=[_qs_orm.optimizer_extension()],
)


# =========================================================================
# Multiple-types schema
# =========================================================================

_multi_orm = StrawberryORM("tortoise")


@_multi_orm.type(TortUser)
class UserBrief:
    id: auto
    name: auto


@_multi_orm.type(TortUser)
class UserFull:
    id: auto
    name: auto
    email: auto


@strawberry.type
class _MultiTypeQuery:
    @strawberry.field
    def users_brief(self) -> list[UserBrief]:
        return TortUser.all()  # type: ignore[return-value]

    @strawberry.field
    def users_full(self) -> list[UserFull]:
        return TortUser.all()  # type: ignore[return-value]


multi_type_schema = strawberry.Schema(
    query=_MultiTypeQuery,
    extensions=[_multi_orm.optimizer_extension()],
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


def _make_result_executor(target_schema):
    async def _execute(query, variables=None):
        return await target_schema.execute(
            query,
            variable_values=variables or {},
        )

    return _execute


@pytest_asyncio.fixture
async def execute(seed):
    return _make_executor(main_schema)


@pytest_asyncio.fixture
async def node_execute(seed):
    return _make_executor(_build_node_mutation_schema())


@pytest_asyncio.fixture
async def node_execute_result(seed):
    return _make_result_executor(_build_node_mutation_schema())


@pytest_asyncio.fixture
async def projected_node_execute(seed):
    return _make_executor(_build_node_mutation_schema())


@pytest_asyncio.fixture
async def projected_node_execute_result(seed):
    return _make_result_executor(_build_node_mutation_schema())


@pytest_asyncio.fixture
async def self_model_execute(seed):
    return _make_executor(self_model_schema)


@pytest_asyncio.fixture
async def get_queryset_execute(seed):
    return _make_executor(get_queryset_schema)


@pytest_asyncio.fixture
async def multi_type_execute(seed):
    return _make_executor(multi_type_schema)


@pytest.fixture
def user_brief_type():
    return UserBrief


@pytest.fixture
def user_full_type():
    return UserFull
