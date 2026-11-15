"""SQLAlchemy models, seeding and execution for the kitchen-sink benchmark."""

from __future__ import annotations

import datetime
import os
import pathlib
import sqlite3

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    event,
    insert,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from benchmarks.kitchen_sink.harness import RECORDER, timed_sqlite_cursor
from strawberry_orm import StrawberryORM

DB_DIR = pathlib.Path(os.environ.get("KS_DB_DIR", "/tmp/ks_bench"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "sqlalchemy.sqlite3"


class Base(DeclarativeBase):
    pass


post_tag = Table(
    "post_tag",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("post.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tag.id"), primary_key=True, index=True),
)


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2))
    founded: Mapped[datetime.date] = mapped_column(Date)

    users: Mapped[list[User]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    organization: Mapped[Organization] = relationship(back_populates="users")
    posts: Mapped[list[Post]] = relationship(back_populates="author")
    comments: Mapped[list[Comment]] = relationship(back_populates="author")


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)

    parent: Mapped[Category | None] = relationship(
        remote_side="Category.id", back_populates="children"
    )
    children: Mapped[list[Category]] = relationship(back_populates="parent")
    posts: Mapped[list[Post]] = relationship(back_populates="category")


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)

    posts: Mapped[list[Post]] = relationship(secondary=post_tag, back_populates="tags")


class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    views: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float] = mapped_column(Float, default=0)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime)

    author: Mapped[User] = relationship(back_populates="posts")
    category: Mapped[Category] = relationship(back_populates="posts")
    tags: Mapped[list[Tag]] = relationship(secondary=post_tag, back_populates="posts")
    comments: Mapped[list[Comment]] = relationship(back_populates="post")


class Comment(Base):
    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    upvotes: Mapped[int] = mapped_column(Integer, default=0)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime)

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped[User] = relationship(back_populates="comments")


orm = StrawberryORM.for_sqlalchemy(
    dialect="sqlite",
    lazy_resolution="off",
    warn_missing_scope=False,
    warn_sensitive=False,
)


def active_users(query):
    return query.where(User.is_active.is_(True))


def visible_comments(query):
    return query.where(Comment.is_flagged.is_(False))


def visible_posts(query):
    return query.where(Post.status != "archived")


def newest_first(query, info):
    return query.order_by(Post.published_at.desc(), Post.id)


def top_comments(post):
    return (
        select(Comment)
        .where(Comment.post_id == post.id, Comment.upvotes >= 5)
        .order_by(Comment.upvotes.desc(), Comment.id)
    )


def posts_by(user):
    return select(Post).where(Post.author_id == user.id)


# -- seeding ------------------------------------------------------------------


class _TimedConnection(sqlite3.Connection):
    cursor_class = timed_sqlite_cursor(sqlite3.Cursor)

    def cursor(self, factory=None):
        return super().cursor(factory or self.cursor_class)


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    creator=lambda: sqlite3.connect(
        str(DB_PATH), factory=_TimedConnection, check_same_thread=False
    ),
)


def is_seeded(expected_posts: int) -> bool:
    try:
        with Session(engine) as session:
            return session.scalar(text("SELECT count(*) FROM post")) == expected_posts
    except Exception:
        return False


def seed(data) -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for model, rows in (
            (Organization, data.organizations),
            (User, data.users),
            (Category, data.categories),
            (Tag, data.tags),
            (Post, data.posts),
            (Comment, data.comments),
        ):
            session.execute(insert(model), rows)
        session.execute(
            insert(post_tag), [{"post_id": p, "tag_id": t} for p, t in data.post_tags]
        )
        session.commit()
        session.execute(text("ANALYZE"))


# -- statement capture ----------------------------------------------------------


def _listen(target) -> None:
    @event.listens_for(target, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        RECORDER.begin(statement)

    @event.listens_for(target, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        RECORDER.end()


def install_recorder() -> None:
    """The sync engine's cursors time themselves; the async one is listened to."""


# -- execution ----------------------------------------------------------------


def make_runner(schema, mode: str):
    if mode == "sync":

        def run(workload):
            with Session(engine) as session:
                result = schema.execute_sync(
                    workload.query,
                    variable_values=workload.variables,
                    context_value={"session": session},
                )
            if result.errors:
                raise RuntimeError(result.errors)
            return result.data

        return "sync", run

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    _listen(async_engine.sync_engine)

    async def run_async(workload):
        async with AsyncSession(async_engine) as session:
            result = await schema.execute(
                workload.query,
                variable_values=workload.variables,
                context_value={"session": session},
            )
        if result.errors:
            raise RuntimeError(result.errors)
        return result.data

    return "async", run_async
