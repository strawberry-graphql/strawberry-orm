"""A ``using=`` resolver over relations a keyed statement cannot read again.

A relation joined on more than its keys is read again row by row, under the
scope. One holding changes the session has not flushed cannot be read again
without losing them, so the resolver is refused rather than shown them.
"""

import pytest
import strawberry
from sqlalchemy import Boolean, ForeignKey, Integer, String, and_, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class RtuAuthor(Base):
    __tablename__ = "rtu_author"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    is_public: Mapped[bool] = mapped_column(Boolean)


class RtuPost(Base):
    __tablename__ = "rtu_post"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    is_published: Mapped[bool] = mapped_column(Boolean)
    author_id: Mapped[int] = mapped_column(ForeignKey("rtu_author.id"))
    author: Mapped[RtuAuthor] = relationship()
    published_by: Mapped[RtuAuthor | None] = relationship(
        primaryjoin=lambda: and_(
            RtuPost.author_id == RtuAuthor.id, RtuPost.is_published.is_(True)
        ),
        viewonly=True,
    )


def _name(author):
    return "nobody" if author is None else author.name


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add_all(
            [
                RtuAuthor(id=1, name="Alice", is_public=True),
                RtuAuthor(id=2, name="Hidden", is_public=False),
                RtuPost(id=1, title="p1", is_published=True, author_id=1),
                RtuPost(id=2, title="p2", is_published=True, author_id=2),
                RtuPost(id=3, title="draft", is_published=False, author_id=1),
            ]
        )
        session.commit()
        session.expunge_all()
        yield session
    engine.dispose()


def _schema(root):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )

    @orm.type(RtuAuthor, name="RtuAuthor")
    class AuthorType:
        name: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(RtuAuthor.is_public.is_(True))

    @orm.type(RtuPost, name="RtuPost")
    class PostType:
        title: auto

        @orm.field.computed(using=["published_by"])
        def credit(self) -> str:
            return _name(self.published_by)

        @orm.field.computed(using=["author"])
        def byline(self) -> str:
            return _name(self.author)

    @strawberry.type
    class Query:
        @orm.field.lazy
        def posts(self, info: strawberry.Info) -> list[PostType]:
            return root(info)

    return orm.schema(query=Query)


def _shown(result, field):
    assert result.errors is None, result.errors
    return {post["title"]: post[field] for post in result.data["posts"]}


def test_a_relation_joined_on_more_than_its_keys_is_read_again_per_row(session):
    schema = _schema(lambda info: select(RtuPost).order_by(RtuPost.id))
    result = schema.execute_sync(
        "{ posts { title credit } }", context_value={"session": session}
    )
    assert _shown(result, "credit") == {
        "p1": "Alice",
        "p2": "nobody",
        "draft": "nobody",
    }


def test_one_the_app_loaded_itself_is_read_again(session):
    """The app's own read fills ``published_by`` unscoped, with Hidden."""
    rows = session.scalars(select(RtuPost).order_by(RtuPost.id)).all()
    assert [_name(row.published_by) for row in rows] == ["Alice", "Hidden", "nobody"]

    schema = _schema(lambda info: rows)
    result = schema.execute_sync(
        "{ posts { title credit } }", context_value={"session": session}
    )
    assert _shown(result, "credit") == {
        "p1": "Alice",
        "p2": "nobody",
        "draft": "nobody",
    }


def test_a_relation_holding_unflushed_changes_is_refused(session):
    rows = session.scalars(select(RtuPost).order_by(RtuPost.id)).all()
    rows[0].author = RtuAuthor(id=3, name="Unsaved", is_public=False)

    schema = _schema(lambda info: rows)
    result = schema.execute_sync(
        "{ posts { title byline } }", context_value={"session": session}
    )
    assert result.errors is not None
    assert all("not yet flushed" in error.message for error in result.errors)
