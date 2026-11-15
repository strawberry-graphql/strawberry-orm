from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import StrawberryORM, auto
from tests.abstract.relation_cache_marks import AbstractTestRelationCacheMarks
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRelationCacheMarks(AbstractTestRelationCacheMarks):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        self.session.expunge_all()
        return schema.execute_sync(query, context_value={"session": self.session})

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)


def published_only(stmt, info):
    return stmt.where(Post.is_published.is_(True))


def schema(orm):
    """``UserType.posts`` keeps only published posts; ``AuthorStats`` reads them all."""

    @orm.type(Post, name="PostTitle")
    class PostTitleType:
        title: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Post.title != "Rust Adventures")

    @orm.type(User, name="AuthorStats")
    class AuthorStatsType:
        name: auto

        @orm.field.computed(using=["posts"])
        def counted(self) -> bool:
            return True

    @orm.type(User)
    class UserType:
        name: auto
        posts: list[PostTitleType] = orm.field.eager(scope=published_only)

    @orm.type(Comment)
    class CommentType:
        author: UserType

    @orm.type(Post)
    class PostType:
        title: auto
        author: AuthorStatsType
        comments: list[CommentType]

    @strawberry.type
    class Query:
        stats: list[AuthorStatsType] = orm.field.eager()
        users: list[UserType] = orm.field.eager()
        posts: list[PostType] = orm.field.eager()

        @strawberry.field
        def user(self, info: strawberry.Info, id: int) -> UserType | None:
            return info.context["session"].get(User, id)

    return orm.schema(query=Query)


def execute(orm, session, query):
    session.expunge_all()
    result = schema(orm).execute_sync(query, context_value={"session": session})
    assert result.errors is None, result.errors
    return result.data


def titles(user):
    return [post["title"] for post in user["posts"]]


@pytest.mark.parametrize(
    "then", ["user(id: 2) { posts { title } }", "users { posts { title } }"]
)
def test_an_identity_mapped_row_reads_its_relation_again_for_a_stricter_type(
    orm, sa_session, seed, then
):
    data = execute(orm, sa_session, f"{{ stats {{ counted }} {then} }}")
    users = data.get("users") or [data["user"]]
    assert all("Draft Post" not in titles(user) for user in users), users


def test_a_relation_one_query_loads_under_two_scopes_is_read_again(
    orm, sa_session, seed
):
    """Bob wrote the draft and commented on a post: as the post's author his
    posts load for ``AuthorStats``, as the commenter for ``UserType``, and his
    row holds whichever loaded first."""
    data = execute(
        orm,
        sa_session,
        "{ posts { author { counted } comments { author { name posts { title } } } } }",
    )
    commenters = [
        comment["author"] for post in data["posts"] for comment in post["comments"]
    ]
    assert {user["name"] for user in commenters} == {"Alice", "Bob", "Charlie"}
    assert all("Draft Post" not in titles(user) for user in commenters), commenters
