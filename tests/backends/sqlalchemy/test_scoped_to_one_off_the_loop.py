"""A to-one relation is read again with its scope on every kind of session.

A resolver may hand back rows whose relation SQLAlchemy has already filled,
unscoped, as a ``lazy="joined"`` mapping does. The relation is then read
again under the related type's scope. On an ``AsyncSession``, or a
``Session`` under ``offload_sync_session``, that read is awaited, and a
to-one relation gets its one row, or ``None``, from it as a sync one does.
"""

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "author"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    hidden: Mapped[bool] = mapped_column(default=False)


class Book(Base):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author | None] = relationship(lazy="joined")


def _rows():
    return [
        Author(id=1, name="Alice"),
        Author(id=2, name="Mallory", hidden=True),
        Book(id=1, title="Seen", author_id=1),
        Book(id=2, title="Unseen", author_id=2),
    ]


def _schema(**options):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, **options
    )

    @orm.type(Author)
    class AuthorType:
        name: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Author.hidden.is_(False))

    @orm.type(Book)
    class BookType:
        title: auto
        author: AuthorType | None

    @strawberry.type
    class Query:
        @strawberry.field
        async def books(self, info: strawberry.Info) -> list[BookType]:
            session = info.context["session"]
            stmt = select(Book).order_by(Book.id)
            if isinstance(session, AsyncSession):
                return list((await session.scalars(stmt)).unique())
            return list(session.scalars(stmt).unique())

    return orm.schema(query=Query)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all(_rows())
        session.commit()
        session.expunge_all()
        yield session
    engine.dispose()


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(_rows())
        await session.commit()
        session.expunge_all()
        yield session
    await engine.dispose()


@pytest.fixture(params=["sync", "async", "offloaded"])
def run(request, session):
    """Run a query on a ``Session``, an ``AsyncSession``, or a ``Session``
    whose statements are sent from a worker thread."""
    if request.param == "async":
        context = {"session": request.getfixturevalue("async_session")}
        schema = _schema()
    else:
        context = {"session": session}
        schema = _schema(offload_sync_session=request.param == "offloaded")

    async def _run(query):
        return await schema.execute(query, context_value=context)

    return _run


async def test_a_to_one_relation_the_mapper_filled_is_read_with_its_scope(run):
    result = await run("{ books { title author { name } } }")

    assert result.errors is None, result.errors
    assert result.data == {
        "books": [
            {"title": "Seen", "author": {"name": "Alice"}},
            {"title": "Unseen", "author": None},
        ]
    }
