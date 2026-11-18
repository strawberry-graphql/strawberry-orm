"""A field the library is asked to build carries everything it was declared with
(SQLAlchemy)."""

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    mapped_column,
    relationship,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.declared_field_options import (
    AbstractTestDeclaredFieldOptions,
    Allow,
)
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def make_orm():
    def _make(**options):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            warn_missing_scope=False,
            **{"lazy_resolution": "off", **options},
        )

    return _make


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        only=lambda query, model, field, value: query.where(
            getattr(model, field) == value
        ),
        exclude=lambda query, model, field, value: query.where(
            getattr(model, field) != value
        ),
    )


@pytest.fixture
def held(sa_session, seed):
    async def _held(model, *ids):
        return [sa_session.get(model, pk) for pk in ids]

    return _held


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, context):
        return schema.execute_sync(
            query, context_value={"session": sa_session, **context}
        )

    return _run


class TestDeclaredFieldOptions(AbstractTestDeclaredFieldOptions):
    pass


class _Base(DeclarativeBase):
    pass


class RdUser(_Base):
    __tablename__ = "rd_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    # Loaded with every user, whether or not the query reads it.
    posts: Mapped[list["RdPost"]] = relationship(
        back_populates="author", lazy="selectin", order_by="RdPost.id"
    )


class RdPost(_Base):
    __tablename__ = "rd_post"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    is_published: Mapped[bool]
    author_id: Mapped[int] = mapped_column(ForeignKey("rd_user.id"))
    author: Mapped[RdUser] = relationship(back_populates="posts")


@pytest.fixture
def rd_session():
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                RdUser(id=1, name="Alice"),
                RdUser(id=2, name="Bob"),
                RdPost(id=1, title="hello", is_published=True, author_id=1),
                RdPost(id=2, title="draft", is_published=False, author_id=1),
                RdPost(id=3, title="bob's", is_published=True, author_id=2),
            ]
        )
        session.commit()
        session.expunge_all()
        yield session
    engine.dispose()


def _rd_schema(declare_posts):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", lazy_resolution="off")

    @orm.type(RdPost)
    class PostType:
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query

    @orm.type(RdUser)
    class UserType:
        name: auto
        posts: list[PostType] = declare_posts(orm)

        @classmethod
        def scope_rows(cls, query, info):
            return query

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.eager()

        @orm.field.lazy
        def aliased_users(self, info: strawberry.Info) -> list[UserType]:
            return select(aliased(RdUser))

    return orm.schema(query=Query)


def _published(query, info):
    return query.where(RdPost.is_published.is_(True))


POSTS = {
    "scope": lambda orm: orm.field.eager(scope=_published),
    "scope_permitted": lambda orm: orm.field.eager(
        scope=_published, permission_classes=[Allow]
    ),
    "scope_filters": lambda orm: orm.field.eager(
        scope=_published, filters=orm.filter(RdPost)
    ),
}

READS = {
    # The first read loads the users, and with them all their posts, unscoped.
    "sibling_preloaded": (
        "users",
        "{ warm: users { name } users { name posts { title } } }",
    ),
    # The planner does not see through an alias it did not build.
    "aliased_root": ("aliasedUsers", "{ aliasedUsers { name posts { title } } }"),
}


@pytest.mark.parametrize("read", list(READS))
@pytest.mark.parametrize("posts", list(POSTS))
def test_a_scoped_relation_holds_over_rows_the_mapper_loaded(rd_session, posts, read):
    root, query = READS[read]
    result = _rd_schema(POSTS[posts]).execute_sync(
        query, context_value={"session": rd_session}
    )
    assert result.errors is None, result.errors
    assert {
        user["name"]: [post["title"] for post in user["posts"]]
        for user in result.data[root]
    } == {"Alice": ["hello"], "Bob": ["bob's"]}
