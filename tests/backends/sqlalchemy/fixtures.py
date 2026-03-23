"""All fixtures for SQLAlchemy integration tests.

Module-level objects:  orm instances, type classes, schemas.
Pytest fixtures:       session, seed, model classes, execute helpers, orm factory.
"""

import pytest
import strawberry
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from strawberry import relay
from strawberry.types.cast import cast as strawberry_cast

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import (
    Base as SABase,
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

# =========================================================================
# Main schema
# =========================================================================

_main_orm = StrawberryORM("sqlalchemy", dialect="sqlite")

UserFilter = _main_orm.filter(SAUser)
UserOrder = _main_orm.order(SAUser)
PostFilter = _main_orm.filter(SAPost)
PostOrder = _main_orm.order(SAPost)
CommentFilter = _main_orm.filter(SAComment)
CommentOrder = _main_orm.order(SAComment)
TagFilter = _main_orm.filter(SATag)
TagOrder = _main_orm.order(SATag)


@_main_orm.type(SAComment, filters=CommentFilter, order=CommentOrder)
class CommentType:
    id: auto
    body: auto
    post_id: auto
    author_id: auto
    parent_id: auto


@_main_orm.type(SATag, filters=TagFilter, order=TagOrder)
class TagType:
    id: auto
    name: auto


@_main_orm.type(SAPost, filters=PostFilter, order=PostOrder)
class PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    tags: list[TagType]
    comments: list[CommentType]


@_main_orm.type(SAUser, filters=UserFilter, order=UserOrder)
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
    SATag, create=CreateTagInput, update=UpdateTagInput, unlink=True, delete=True
)


@strawberry.type
class _MainQuery:
    users: list[UserType] = _main_orm.field()
    posts: list[PostType] = _main_orm.field()
    comments: list[CommentType] = _main_orm.field()

    @strawberry.field
    def user(self, info: strawberry.types.Info, id: int) -> UserType | None:
        from sqlalchemy.orm import Session

        session: Session = info.context["session"]
        return session.get(SAUser, id)


@strawberry.type
class _MainMutation:
    @strawberry.mutation
    def create_post(
        self, info: strawberry.types.Info, input: CreatePostInput
    ) -> PostType:
        from sqlalchemy.orm import Session

        session: Session = info.context["session"]
        post = SAPost(
            title=input.title,
            body=input.body,
            is_published=input.is_published,
            author_id=input.author_id,
        )
        session.add(post)
        session.commit()
        return post  # type: ignore[return-value]

    @strawberry.mutation
    def update_post(
        self, info: strawberry.types.Info, input: UpdatePostInput
    ) -> PostType | None:
        from sqlalchemy.orm import Session

        session: Session = info.context["session"]
        post = session.get(SAPost, input.id)
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
        session.commit()
        return post  # type: ignore[return-value]

    @strawberry.mutation
    def delete_post(self, info: strawberry.types.Info, id: int) -> bool:
        from sqlalchemy.orm import Session

        session: Session = info.context["session"]
        post = session.get(SAPost, id)
        if post is None:
            return False
        session.delete(post)
        session.commit()
        return True

    @strawberry.mutation
    def set_post_tags(
        self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
    ) -> PostType | None:
        from sqlalchemy.orm import Session

        session: Session = info.context["session"]
        post = session.get(SAPost, post_id)
        if post is None:
            return None
        _main_orm.apply_ref_list(post, "tags", tags, info)
        session.commit()
        return post  # type: ignore[return-value]


main_schema = strawberry.Schema(
    query=_MainQuery,
    mutation=_MainMutation,
    extensions=[_main_orm.optimizer_extension()],
)


# =========================================================================
# Node mutation schema
# =========================================================================

_node_orm = StrawberryORM("sqlalchemy", dialect="sqlite")
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


@_node_orm.type(SAUser)
class UserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_node_orm.type(SATag)
class TagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_node_orm.type(SAComment)
class CommentNode(relay.Node):
    id: relay.NodeID[int]
    body: auto

    @strawberry.field
    def author(self) -> UserNode:
        return strawberry_cast(UserNode, self.author)


@_node_orm.type(SAPost)
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
        return [strawberry_cast(TagNode, tag) for tag in self.tags]

    @strawberry.field
    def comments(self) -> list[CommentNode]:
        return [
            strawberry_cast(CommentNode, comment)
            for comment in sorted(self.comments, key=lambda comment: comment.id)
        ]


@strawberry.type(name="Query")
class _NodeQuery:
    @strawberry.field
    def users(self, info: strawberry.types.Info) -> list[UserNode]:
        session = info.context["session"]
        return [
            strawberry_cast(UserNode, user)
            for user in session.execute(select(SAUser).order_by(SAUser.id))
            .scalars()
            .all()
        ]

    @strawberry.field
    def posts(self, info: strawberry.types.Info) -> list[PostNode]:
        session = info.context["session"]
        return [
            strawberry_cast(PostNode, post)
            for post in session.execute(select(SAPost).order_by(SAPost.id))
            .scalars()
            .all()
        ]

    @strawberry.field
    def comments(self, info: strawberry.types.Info) -> list[CommentNode]:
        session = info.context["session"]
        return [
            strawberry_cast(CommentNode, comment)
            for comment in session.execute(select(SAComment).order_by(SAComment.id))
            .scalars()
            .all()
        ]

    @strawberry.field
    def tags(self, info: strawberry.types.Info) -> list[TagNode]:
        session = info.context["session"]
        return [
            strawberry_cast(TagNode, tag)
            for tag in session.execute(select(SATag).order_by(SATag.id)).scalars().all()
        ]


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


node_mutation_schema = strawberry.Schema(
    query=_NodeQuery,
    mutation=_NodeMutation,
    extensions=[_node_orm.optimizer_extension()],
)


# =========================================================================
# Self-is-model schema
# =========================================================================

_self_orm = StrawberryORM("sqlalchemy", dialect="sqlite")


@_self_orm.type(SAPost)
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


@_self_orm.type(SAUser)
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
        return len(self.posts)


@strawberry.type
class _SelfModelQuery:
    @strawberry.field
    def users(self, info: strawberry.types.Info) -> list[UserWithCustom]:
        return select(SAUser)  # type: ignore[return-value]

    @strawberry.field
    def posts(self, info: strawberry.types.Info) -> list[PostWithSummary]:
        return select(SAPost)  # type: ignore[return-value]


self_model_schema = strawberry.Schema(
    query=_SelfModelQuery,
    extensions=[_self_orm.optimizer_extension()],
)


# =========================================================================
# Get-queryset schema
# =========================================================================

_qs_orm = StrawberryORM("sqlalchemy", dialect="sqlite")


@_qs_orm.type(SAPost)
class PublishedPostType:
    id: auto
    title: auto
    is_published: auto

    @classmethod
    def get_queryset(cls, stmt, info):
        return stmt.where(SAPost.is_published == True)  # noqa: E712


@_qs_orm.type(SAUser)
class UnscopedUserType:
    id: auto
    name: auto


@strawberry.type
class _GetQuerysetQuery:
    @strawberry.field
    def posts(self, info: strawberry.types.Info) -> list[PublishedPostType]:
        return select(SAPost)  # type: ignore[return-value]

    @strawberry.field
    def users(self, info: strawberry.types.Info) -> list[UnscopedUserType]:
        return select(SAUser)  # type: ignore[return-value]


get_queryset_schema = strawberry.Schema(
    query=_GetQuerysetQuery,
    extensions=[_qs_orm.optimizer_extension()],
)


# =========================================================================
# Multiple-types schema
# =========================================================================

_multi_orm = StrawberryORM("sqlalchemy", dialect="sqlite")


@_multi_orm.type(SAUser)
class UserBrief:
    id: auto
    name: auto


@_multi_orm.type(SAUser)
class UserFull:
    id: auto
    name: auto
    email: auto


@strawberry.type
class _MultiTypeQuery:
    @strawberry.field
    def users_brief(self, info: strawberry.types.Info) -> list[UserBrief]:
        return select(SAUser)  # type: ignore[return-value]

    @strawberry.field
    def users_full(self, info: strawberry.types.Info) -> list[UserFull]:
        return select(SAUser)  # type: ignore[return-value]


multi_type_schema = strawberry.Schema(
    query=_MultiTypeQuery,
    extensions=[_multi_orm.optimizer_extension()],
)


# =========================================================================
# Pytest fixtures
# =========================================================================

# -- Fresh ORM instance (for tests that build their own types) ---------------


@pytest.fixture
def orm():
    return StrawberryORM("sqlalchemy", dialect="sqlite")


# -- Model class fixtures ----------------------------------------------------


@pytest.fixture
def User():
    return SAUser


@pytest.fixture
def Post():
    return SAPost


@pytest.fixture
def Tag():
    return SATag


@pytest.fixture
def Comment():
    return SAComment


# -- Session / DB fixtures ---------------------------------------------------


@pytest.fixture
def sa_session():
    engine = create_engine("sqlite:///:memory:")
    SABase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# -- Seed fixture (returns instances) ----------------------------------------


@pytest.fixture
def seed(sa_session):
    alice = SAUser(id=1, name="Alice", email="alice@example.com")
    bob = SAUser(id=2, name="Bob", email="bob@example.com")
    charlie = SAUser(id=3, name="Charlie", email="charlie@test.org")
    sa_session.add_all([alice, bob, charlie])
    sa_session.flush()

    python = SATag(id=1, name="python")
    graphql = SATag(id=2, name="graphql")
    rust = SATag(id=3, name="rust")
    sa_session.add_all([python, graphql, rust])
    sa_session.flush()

    p1 = SAPost(
        id=1, title="Hello World", body="First post", is_published=True, author_id=1
    )
    p2 = SAPost(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author_id=1,
    )
    p3 = SAPost(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author_id=2,
    )
    p4 = SAPost(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author_id=3,
    )
    sa_session.add_all([p1, p2, p3, p4])
    sa_session.flush()

    p1.tags.append(python)
    p2.tags.extend([python, graphql])
    p4.tags.extend([rust])
    sa_session.flush()

    c1 = SAComment(id=1, body="Nice post!", post_id=1, author_id=2)
    c2 = SAComment(id=2, body="Thanks!", post_id=1, author_id=1, parent_id=1)
    c3 = SAComment(id=3, body="Great guide", post_id=2, author_id=3)
    sa_session.add_all([c1, c2, c3])
    sa_session.commit()

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


# -- Execute fixtures --------------------------------------------------------


def _make_executor(target_schema, sa_session):
    def _execute(query, variables=None):
        result = target_schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": sa_session},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute


def _make_result_executor(target_schema, sa_session):
    def _execute(query, variables=None):
        return target_schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": sa_session},
        )

    return _execute


@pytest.fixture
def execute(sa_session):
    return _make_executor(main_schema, sa_session)


@pytest.fixture
def node_execute(sa_session, seed):
    return _make_executor(node_mutation_schema, sa_session)


@pytest.fixture
def node_execute_result(sa_session, seed):
    return _make_result_executor(node_mutation_schema, sa_session)


@pytest.fixture
def projected_node_execute(sa_session, seed):
    return _make_executor(node_mutation_schema, sa_session)


@pytest.fixture
def projected_node_execute_result(sa_session, seed):
    return _make_result_executor(node_mutation_schema, sa_session)


@pytest.fixture
def self_model_execute(sa_session, seed):
    return _make_executor(self_model_schema, sa_session)


@pytest.fixture
def get_queryset_execute(sa_session, seed):
    return _make_executor(get_queryset_schema, sa_session)


@pytest.fixture
def multi_type_execute(sa_session, seed):
    return _make_executor(multi_type_schema, sa_session)


@pytest.fixture
def user_brief_type():
    return UserBrief


@pytest.fixture
def user_full_type():
    return UserFull


# -- Query counter (for optimizer tests) -------------------------------------


@pytest.fixture
def query_counter(sa_session):
    """Track SQL statements executed during a test."""
    queries: list[str] = []

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        queries.append(statement)

    engine = sa_session.bind
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    yield queries
    event.remove(engine, "before_cursor_execute", _before_cursor_execute)
