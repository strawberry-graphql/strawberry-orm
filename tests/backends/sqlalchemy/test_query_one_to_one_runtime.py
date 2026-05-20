"""One-to-one query shapes that exercise optimizer relation branches."""

from typing import Optional

import pytest
import strawberry
from sqlalchemy import Boolean, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "runtime_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))

    posts: Mapped[list["Post"]] = relationship(back_populates="author")
    profile: Mapped[Optional["Profile"]] = relationship(back_populates="user")


class Post(Base):
    __tablename__ = "runtime_post"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("runtime_user.id"))

    author: Mapped[User] = relationship(back_populates="posts")


class Profile(Base):
    __tablename__ = "runtime_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bio: Mapped[str] = mapped_column(String(200))
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("runtime_user.id"), unique=True)

    user: Mapped[User] = relationship(back_populates="profile")


@pytest.fixture
def runtime_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        alice = User(id=1, name="Alice", email="alice@example.com")
        bob = User(id=2, name="Bob", email="bob@example.com")
        charlie = User(id=3, name="Charlie", email="charlie@test.org")
        session.add_all([alice, bob, charlie])
        session.flush()

        session.add_all(
            [
                Post(id=1, title="Hello World", is_published=True, author=alice),
                Post(id=2, title="GraphQL Guide", is_published=True, author=alice),
                Post(id=3, title="Draft Post", is_published=False, author=bob),
                Post(id=4, title="Rust Adventures", is_published=True, author=charlie),
            ]
        )
        session.add_all(
            [
                Profile(id=1, bio="Alice Bio", is_public=True, user=alice),
                Profile(id=2, bio="Bob Bio", is_public=False, user=bob),
                Profile(id=3, bio="Charlie Bio", is_public=True, user=charlie),
            ]
        )
        session.commit()
        yield session
    finally:
        session.close()


class TestQueryOneToOneRuntime:
    def test_root_users_can_select_reverse_one_to_one(self, runtime_session):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Profile)
        class ProfileType:
            id: auto
            bio: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            profile: ProfileType | None

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name profile { bio } } }",
            context_value={"session": runtime_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "profile": {"bio": "Alice Bio"}},
                {"name": "Bob", "profile": {"bio": "Bob Bio"}},
                {"name": "Charlie", "profile": {"bio": "Charlie Bio"}},
            ]
        }

    def test_prefetched_reverse_relations_can_select_nested_one_to_one(
        self, runtime_session
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Profile)
        class ProfileType:
            id: auto
            bio: auto

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto
            profile: ProfileType | None

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title author { name profile { bio } } } } }",
            context_value={"session": runtime_session},
        )
        assert result.errors is None
        assert result.data["users"][0]["posts"][0]["author"]["profile"] == {
            "bio": "Alice Bio"
        }
        assert result.data["users"][1]["posts"][0]["author"]["profile"] == {
            "bio": "Bob Bio"
        }

    def test_reverse_one_to_one_respects_custom_queryset(self, runtime_session):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Profile)
        class ProfileType:
            id: auto
            bio: auto

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(Profile.is_public.is_(True))

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            profile: ProfileType | None

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name profile { bio } } }",
            context_value={"session": runtime_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "profile": {"bio": "Alice Bio"}},
                {"name": "Bob", "profile": None},
                {"name": "Charlie", "profile": {"bio": "Charlie Bio"}},
            ]
        }
