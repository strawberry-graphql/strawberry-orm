"""A write decides on the rows of the database it writes to.

A SQLAlchemy app routes its own reads, in its session's ``get_bind``: these
sessions send reads to the replica except while ``is_writing()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Delete,
    ForeignKey,
    Insert,
    String,
    Update,
    create_engine,
    insert,
    select,
)
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from strawberry_orm import is_writing
from tests.abstract.write_path_authorization import resolve
from tests.abstract.writes_read_where_they_write import (
    ME,
    AbstractTestAWriteReadsWhereItWrites,
    ReplicaDb,
    repos,
    seed,
    state,
    write,
)
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class RwTag(Base):
    __tablename__ = "rw_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(50))


class RwDoc(Base):
    __tablename__ = "rw_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(50))
    notes: Mapped[list[RwNote]] = relationship(back_populates="doc")
    tags: Mapped[list[RwTag]] = relationship(secondary="rw_tagging")


class RwNote(Base):
    __tablename__ = "rw_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[str] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("rw_doc.id"))
    doc: Mapped[RwDoc | None] = relationship(back_populates="notes")


class RwGrant(Base):
    __tablename__ = "rw_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int]
    user: Mapped[str] = mapped_column(String(50))


class RwTagging(Base):
    __tablename__ = "rw_tagging"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("rw_doc.id"))
    tag_id: Mapped[int] = mapped_column(ForeignKey("rw_tag.id"))
    owner: Mapped[str] = mapped_column(String(50))


class RwDocTitle(Base):
    """A select of docs' titles: no table a write is routed for."""

    __table__ = select(RwDoc.id, RwDoc.title).subquery()


class _Models:
    Tag = RwTag
    Doc = RwDoc
    Note = RwNote
    Grant = RwGrant
    Tagging = RwTagging


def _writes(session: Session, clause: Any) -> bool:
    return session._flushing or isinstance(clause, (Insert, Update, Delete))


class RoutingSession(Session):
    """Reads from the replica, except while the library decides a write."""

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kw):
        if bind is not None:
            return bind
        if is_writing() or _writes(self, clause):
            return self.info["primary"]
        return self.info["replica"]


class ReplicaSession(Session):
    """Reads from the replica whatever the library is doing."""

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kw):
        if bind is not None:
            return bind
        return self.info["primary" if _writes(self, clause) else "replica"]


class DocsOnlySession(ReplicaSession):
    """Binds docs alone, as a session given ``binds={RwDoc: ...}`` does, and
    reads them where it writes them."""

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kw):
        if bind is not None:
            return bind
        if mapper is None or mapper.class_ is not RwDoc:
            raise UnboundExecutionError("Could not locate a bind")
        return self.info["primary"]


@dataclass
class SQLAlchemyReplicaDb(SQLAlchemyWriteDb, ReplicaDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.where(model.owner == ME)

    def granted(self, doc_id: int) -> Any:
        statement = select(RwGrant.id).where(
            RwGrant.doc_id == doc_id, RwGrant.user == ME
        )
        if not self.async_hooks:
            return self.session.execute(statement).first() is not None

        async def found() -> bool:
            return (await self.session.execute(statement)).first() is not None

        return found()

    def _engine(self, database: str) -> Any:
        return self.session.info[database]

    async def create_in(self, database: str, model: type, **values: Any) -> None:
        bind = {"bind": self._engine(database)}
        await resolve(
            self.session.execute(insert(model).values(**values), bind_arguments=bind)
        )
        await resolve(self.session.commit())

    async def primary_rows(self, model: type) -> list[Any]:
        statement = (
            select(model).order_by(model.id).execution_options(populate_existing=True)
        )
        result = await resolve(
            self.session.execute(
                statement, bind_arguments={"bind": self._engine("primary")}
            )
        )
        return list(result.scalars().all())

    async def fetch(self, model: type, pk: int) -> Any:
        statement = (
            select(model)
            .where(model.id == pk)
            .execution_options(populate_existing=True)
        )
        return (await resolve(self.session.execute(statement))).scalar_one()


@pytest_asyncio.fixture
async def make_db():
    """A database pair in each mode, read through sessions of *session_class*."""
    disposals = []

    async def make(mode: str, session_class: type) -> SQLAlchemyReplicaDb:
        if mode == "sync":
            engines = {
                name: create_engine("sqlite://", poolclass=StaticPool)
                for name in ("primary", "replica")
            }
            for engine in engines.values():
                Base.metadata.create_all(engine)
            session = sessionmaker(class_=session_class, info=engines)()
            disposals.append(session.close)
            return SQLAlchemyReplicaDb(_Models, async_hooks=False, session=session)
        pytest.importorskip("greenlet")
        engines = {
            name: create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
            for name in ("primary", "replica")
        }
        for engine in engines.values():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        sync_engines = {name: engine.sync_engine for name, engine in engines.items()}
        session = async_sessionmaker(
            sync_session_class=session_class, info=sync_engines
        )()
        disposals.append(session.close)
        return SQLAlchemyReplicaDb(_Models, async_hooks=True, session=session)

    yield make
    for dispose in disposals:
        await resolve(dispose())


@pytest_asyncio.fixture(params=["sync", "async"])
async def db(request, make_db):
    return await make_db(request.param, RoutingSession)


class TestAWriteReadsWhereItWrites(AbstractTestAWriteReadsWhereItWrites):
    pass


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("entry", ["upsert update", "ref-list update"])
async def test_a_session_reading_apart_from_where_it_writes_is_refused(
    make_db, mode, entry
):
    """A session that reads the replica while a write decides would have it
    decide on rows as they were: the write is refused, and writes nothing."""
    db = await make_db(mode, ReplicaSession)
    await seed(db, "primary", True)
    await seed(db, "replica", False)
    before = await state(db)
    orm = db.new_orm(repos=repos(db))
    with pytest.raises(TypeError, match="reads rows from another database") as caught:
        await write(db, orm, entry)
    assert str(caught.value.__cause__).startswith("RwDoc is read from another")
    assert await state(db) == before


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_session_is_asked_only_about_the_models_it_binds(make_db, mode):
    db = await make_db(mode, DocsOnlySession)
    await seed(db, "primary", False)
    await write(db, db.new_orm(), "upsert update")
    docs, _notes, _taggings = await state(db)
    assert docs[0] == (1, ME, "edited")
