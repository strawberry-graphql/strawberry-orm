"""All fixtures for Django integration tests.

Module-level objects:  orm instances, type classes, schemas.
Pytest fixtures:       seed, model classes, execute helpers, orm factory.

NOTE: Django must be configured before this module is imported.
conftest.py handles that via settings.configure() + django.setup().
"""

import pytest
import strawberry
from django.db import connection
from strawberry import relay
from strawberry.types.cast import cast as strawberry_cast

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.django.models import (
    Comment as DjComment,
)
from tests.backends.django.models import (
    Post as DjPost,
)
from tests.backends.django.models import (
    Publisher as DjPublisher,
)
from tests.backends.django.models import (
    Tag as DjTag,
)
from tests.backends.django.models import (
    User as DjUser,
)

# =========================================================================
# Main schema
# =========================================================================

_main_orm = StrawberryORM.for_django(lazy_resolution="off")

UserFilter = _main_orm.filter(DjUser)
UserOrder = _main_orm.order(DjUser)
PostFilter = _main_orm.filter(DjPost)
PostOrder = _main_orm.order(DjPost)
CommentFilter = _main_orm.filter(DjComment)
CommentOrder = _main_orm.order(DjComment)
TagFilter = _main_orm.filter(DjTag)
TagOrder = _main_orm.order(DjTag)


@_main_orm.type(DjComment, filters=CommentFilter, order=CommentOrder)
class CommentType:
    id: auto
    body: auto
    post_id: auto
    author_id: auto
    parent_id: auto


@_main_orm.type(DjTag, filters=TagFilter, order=TagOrder)
class TagType:
    id: auto
    name: auto


@_main_orm.type(DjPost, filters=PostFilter, order=PostOrder)
class PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    tags: list[TagType]
    comments: list[CommentType]


@_main_orm.type(DjUser, filters=UserFilter, order=UserOrder)
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
    title: str | None = strawberry.UNSET
    body: str | None = strawberry.UNSET
    is_published: bool | None = strawberry.UNSET


TagRef = _main_orm.ref(
    DjTag, create=CreateTagInput, update=UpdateTagInput, unlink=True, delete=True
)


@strawberry.type
class _MainQuery:
    users: list[UserType] = _main_orm.field()
    posts: list[PostType] = _main_orm.field()
    comments: list[CommentType] = _main_orm.field()

    @strawberry.field
    def user(self, id: int) -> UserType | None:
        try:
            return DjUser.objects.get(pk=id)  # type: ignore[return-value]
        except DjUser.DoesNotExist:
            return None


@strawberry.type
class _MainMutation:
    @strawberry.mutation
    def create_post(self, input: CreatePostInput) -> PostType:
        post = DjPost.objects.create(
            title=input.title,
            body=input.body,
            is_published=input.is_published,
            author_id=input.author_id,
        )
        return post  # type: ignore[return-value]

    @strawberry.mutation
    def update_post(self, input: UpdatePostInput) -> PostType | None:
        try:
            post = DjPost.objects.get(pk=input.id)
        except DjPost.DoesNotExist:
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
        post.save()
        return post  # type: ignore[return-value]

    @strawberry.mutation
    def delete_post(self, id: int) -> bool:
        try:
            post = DjPost.objects.get(pk=id)
        except DjPost.DoesNotExist:
            return False
        post.delete()
        return True

    @strawberry.mutation
    def set_post_tags(
        self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
    ) -> PostType | None:
        try:
            post = DjPost.objects.get(pk=post_id)
        except DjPost.DoesNotExist:
            return None
        _main_orm.apply_ref_list(post, "tags", tags, info)
        return post  # type: ignore[return-value]


main_schema = _main_orm.schema(
    query=_MainQuery,
    mutation=_MainMutation,
)


# =========================================================================
# Node mutation schema
# =========================================================================

_node_orm = StrawberryORM.for_django(lazy_resolution="off")
_node_project = {
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


@_node_orm.type(DjUser)
class UserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_node_orm.type(DjTag)
class TagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_node_orm.type(DjComment)
class CommentNode(relay.Node):
    id: relay.NodeID[int]
    body: auto

    @strawberry.field
    def author(self) -> UserNode:
        return strawberry_cast(UserNode, self.author)


@_node_orm.type(DjPost)
class PostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto
    is_published: auto

    @strawberry.field
    def author(self) -> UserNode:
        return strawberry_cast(UserNode, self.author)

    @strawberry.field
    def tags(self) -> list[TagNode]:
        return [strawberry_cast(TagNode, tag) for tag in self.tags.all()]

    @strawberry.field
    def comments(self) -> list[CommentNode]:
        return [
            strawberry_cast(CommentNode, comment)
            for comment in self.comments.all().order_by("id")
        ]


@strawberry.type(name="Query")
class _NodeQuery:
    @strawberry.field
    def users(self) -> list[UserNode]:
        return [
            strawberry_cast(UserNode, user) for user in DjUser.objects.order_by("id")
        ]

    @strawberry.field
    def posts(self) -> list[PostNode]:
        return [
            strawberry_cast(PostNode, post) for post in DjPost.objects.order_by("id")
        ]

    @strawberry.field
    def comments(self) -> list[CommentNode]:
        return [
            strawberry_cast(CommentNode, comment)
            for comment in DjComment.objects.order_by("id")
        ]

    @strawberry.field
    def tags(self) -> list[TagNode]:
        return [strawberry_cast(TagNode, tag) for tag in DjTag.objects.order_by("id")]


def _selected_root_key(input_obj: object) -> str:
    for field_name in input_obj.__class__.__dataclass_fields__:
        if getattr(input_obj, field_name) is not strawberry.UNSET:
            return field_name
    raise ValueError("Exactly one root model must be selected")


_CreateNodeInput = _node_orm.mutations.create_node_input(name="CreateNodeInput")
_UpdateNodeInput = _node_orm.mutations.update_node_input(name="UpdateNodeInput")


@strawberry.type(name="Mutation")
class _NodeMutation:
    create_node = _node_orm.mutations.create_node(input_name="CreateNodeInput")
    update_node = _node_orm.mutations.update_node(input_name="UpdateNodeInput")
    projected_create_node = _node_orm.mutations.create_node(
        project=_node_project,
        input_name="ProjectedCreateNodeInput",
    )
    projected_update_node = _node_orm.mutations.update_node(
        project=_node_project,
        input_name="ProjectedUpdateNodeInput",
    )

    @strawberry.field
    def inspect_create_node_input(self, input: _CreateNodeInput) -> str:
        return _selected_root_key(input)

    @strawberry.field
    def inspect_update_node_input(self, input: _UpdateNodeInput) -> str:
        return _selected_root_key(input)


node_mutation_schema = _node_orm.schema(
    query=_NodeQuery,
    mutation=_NodeMutation,
)


# =========================================================================
# Self-is-model schema
# =========================================================================

_self_orm = StrawberryORM.for_django(lazy_resolution="off")


@_self_orm.type(DjPost)
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


@_self_orm.type(DjUser)
class UserWithCustom:
    id: auto
    name: auto
    email: auto
    posts: list[PostWithSummary]

    @strawberry.field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"

    @strawberry.field
    def post_count(self) -> int:
        return self.posts.count()


@strawberry.type
class _SelfModelQuery:
    @strawberry.field
    def users(self) -> list[UserWithCustom]:
        return DjUser.objects.all()  # type: ignore[return-value]

    @strawberry.field
    def posts(self) -> list[PostWithSummary]:
        return DjPost.objects.all()  # type: ignore[return-value]


self_model_schema = _self_orm.schema(
    query=_SelfModelQuery,
)


# =========================================================================
# Get-queryset schema
# =========================================================================

_qs_orm = StrawberryORM.for_django(lazy_resolution="off")


@_qs_orm.type(DjPost)
class PublishedPostType:
    id: auto
    title: auto
    is_published: auto

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)


@_qs_orm.type(DjUser)
class UnscopedUserType:
    id: auto
    name: auto


@strawberry.type
class _GetQuerysetQuery:
    @strawberry.field
    def posts(self) -> list[PublishedPostType]:
        return DjPost.objects.all()  # type: ignore[return-value]

    @strawberry.field
    def users(self) -> list[UnscopedUserType]:
        return DjUser.objects.all()  # type: ignore[return-value]


get_queryset_schema = _qs_orm.schema(
    query=_GetQuerysetQuery,
)


# =========================================================================
# Multiple-types schema
# =========================================================================

_multi_orm = StrawberryORM.for_django(lazy_resolution="off")


@_multi_orm.type(DjUser)
class UserBrief:
    id: auto
    name: auto


@_multi_orm.type(DjUser)
class UserFull:
    id: auto
    name: auto
    email: auto


@strawberry.type
class _MultiTypeQuery:
    @strawberry.field
    def users_brief(self) -> list[UserBrief]:
        return DjUser.objects.all()  # type: ignore[return-value]

    @strawberry.field
    def users_full(self) -> list[UserFull]:
        return DjUser.objects.all()  # type: ignore[return-value]


multi_type_schema = _multi_orm.schema(
    query=_MultiTypeQuery,
)


# =========================================================================
# Pytest fixtures
# =========================================================================

# -- Fresh ORM instance (for tests that build their own types) ---------------


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off")


# -- Model class fixtures ----------------------------------------------------


@pytest.fixture
def User():
    return DjUser


@pytest.fixture
def Post():
    return DjPost


@pytest.fixture
def Tag():
    return DjTag


@pytest.fixture
def Comment():
    return DjComment


@pytest.fixture
def Publisher():
    return DjPublisher


# -- DB setup fixtures -------------------------------------------------------


def _ensure_tables():
    """Create model tables if they don't exist yet."""
    existing = set(connection.introspection.table_names())
    models = [DjUser, DjTag, DjPost, DjComment]
    to_create = [m for m in models if m._meta.db_table not in existing]
    if not to_create:
        return
    with connection.schema_editor() as editor:
        for model in to_create:
            editor.create_model(model)


def _flush_tables():
    """Delete all data from our custom test tables and reset auto-increment."""
    DjPost.tags.through.objects.all().delete()
    DjComment.objects.all().delete()
    DjPost.objects.all().delete()
    DjTag.objects.all().delete()
    DjUser.objects.all().delete()
    with connection.cursor() as cursor:
        for table in ("user", "post", "tag", "comment"):
            cursor.execute("DELETE FROM sqlite_sequence WHERE name = %s", [table])


@pytest.fixture(autouse=True)
def setup_tables(transactional_db):
    """Ensure tables exist and are clean for each test."""
    _ensure_tables()
    yield
    _flush_tables()


# -- Seed fixture (returns instances) ----------------------------------------


@pytest.fixture
def seed(setup_tables):
    alice = DjUser.objects.create(id=1, name="Alice", email="alice@example.com")
    bob = DjUser.objects.create(id=2, name="Bob", email="bob@example.com")
    charlie = DjUser.objects.create(id=3, name="Charlie", email="charlie@test.org")

    python_tag = DjTag.objects.create(id=1, name="python")
    graphql_tag = DjTag.objects.create(id=2, name="graphql")
    rust_tag = DjTag.objects.create(id=3, name="rust")

    p1 = DjPost.objects.create(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = DjPost.objects.create(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    p3 = DjPost.objects.create(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = DjPost.objects.create(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )

    p1.tags.add(python_tag)
    p2.tags.add(python_tag, graphql_tag)
    p4.tags.add(rust_tag)

    c1 = DjComment.objects.create(id=1, body="Nice post!", post=p1, author=bob)
    c2 = DjComment.objects.create(
        id=2, body="Thanks!", post=p1, author=alice, parent_id=1
    )
    c3 = DjComment.objects.create(id=3, body="Great guide", post=p2, author=charlie)

    return {
        "users": {"alice": alice, "bob": bob, "charlie": charlie},
        "tags": {"python": python_tag, "graphql": graphql_tag, "rust": rust_tag},
        "posts": {
            "hello_world": p1,
            "graphql_guide": p2,
            "draft": p3,
            "rust_adventures": p4,
        },
        "comments": {"nice_post": c1, "thanks": c2, "great_guide": c3},
    }


# -- Execute fixtures --------------------------------------------------------


def _make_executor(target_schema):
    def _execute(query, variables=None):
        result = target_schema.execute_sync(
            query,
            variable_values=variables or {},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


def _make_result_executor(target_schema):
    def _execute(query, variables=None):
        return target_schema.execute_sync(
            query,
            variable_values=variables or {},
        )

    return _execute


@pytest.fixture
def execute(seed):
    return _make_executor(main_schema)


@pytest.fixture
def node_execute(seed):
    return _make_executor(node_mutation_schema)


@pytest.fixture
def node_execute_result(seed):
    return _make_result_executor(node_mutation_schema)


@pytest.fixture
def projected_node_execute(seed):
    return _make_executor(node_mutation_schema)


@pytest.fixture
def projected_node_execute_result(seed):
    return _make_result_executor(node_mutation_schema)


@pytest.fixture
def self_model_execute(seed):
    return _make_executor(self_model_schema)


@pytest.fixture
def get_queryset_execute(seed):
    return _make_executor(get_queryset_schema)


@pytest.fixture
def multi_type_execute(seed):
    return _make_executor(multi_type_schema)


@pytest.fixture
def user_brief_type():
    return UserBrief


@pytest.fixture
def user_full_type():
    return UserFull
