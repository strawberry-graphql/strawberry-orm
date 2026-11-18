"""A write saves the change it decided on, and nothing it only read."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    ForeignKey,
    String,
    create_engine,
    delete,
    inspect,
    select,
    update,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm.repo import _get_sa_pk_column
from tests.abstract.write_path_authorization import member, resolve
from tests.abstract.write_path_concurrent_changes import (
    AbstractTestAWriteSavesOnlyWhatItDecided,
    AbstractTestWhatAWriteSaves,
    Race,
    RaceDb,
    edit,
    racing_orm,
    seed,
    state,
)
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class CwOwner(Base):
    __tablename__ = "cw_owner"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    tier: Mapped[int] = mapped_column(default=0)
    # Without a discriminator, holding a sealed doc takes this.
    docs: Mapped[list[CwDoc]] = relationship(
        back_populates="owner", enable_typechecks=False
    )


class CwDoc(Base):
    __tablename__ = "cw_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    is_published: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("cw_owner.id"))
    owner: Mapped[CwOwner] = relationship(back_populates="docs")
    edited_at: Mapped[datetime] = mapped_column(
        default=datetime.now, onupdate=datetime.now
    )
    notes: Mapped[list[CwNote]] = relationship(back_populates="doc")
    badge: Mapped[CwBadge | None] = relationship(back_populates="doc")


class CwSealedDoc(CwDoc):
    __tablename__ = "cw_sealed_doc"

    id: Mapped[int] = mapped_column(ForeignKey("cw_doc.id"), primary_key=True)
    seal: Mapped[str] = mapped_column(String(50), default="")


class CwNote(Base):
    __tablename__ = "cw_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(50))
    flagged: Mapped[bool] = mapped_column(default=False)
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("cw_doc.id"))
    doc: Mapped[CwDoc | None] = relationship(back_populates="notes")


class CwBadge(Base):
    __tablename__ = "cw_badge"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("cw_doc.id"), unique=True)
    doc: Mapped[CwDoc | None] = relationship(back_populates="badge")


MODELS = SimpleNamespace(
    Owner=CwOwner, Doc=CwDoc, SealedDoc=CwSealedDoc, Note=CwNote, Badge=CwBadge
)


@dataclass
class SQLAlchemyRaceDb(SQLAlchemyWriteDb, RaceDb):
    def change(self, model: type, pk: int, **values: Any) -> Any:
        return self._run(
            update(table)
            .where(table.c.id == pk)
            .values({name: value for name, value in values.items() if name in table.c})
            for table in inspect(model).tables
            if any(name in table.c for name in values)
        )

    def remove(self, model: type, pk: int) -> Any:
        return self._run(
            delete(table).where(table.c.id == pk)
            for table in reversed(inspect(model).tables)
        )

    def _run(self, statements: Any) -> Any:
        """Run *statements* on the session's connection, where its write runs."""
        statements = list(statements)
        if not self.async_hooks:
            for statement in statements:
                self.session.execute(statement)
            return None

        async def run() -> None:
            for statement in statements:
                await self.session.execute(statement)

        return run()

    async def find(self, model: type, pk: int) -> Any:
        statement = (
            select(model)
            .where(_get_sa_pk_column(model) == pk)
            .execution_options(populate_existing=True)
        )
        result = await resolve(self.session.execute(statement))
        return result.scalars().first()


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SQLAlchemyRaceDb(MODELS, async_hooks=False, session=session)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SQLAlchemyRaceDb(MODELS, async_hooks=True, session=session)
    await engine.dispose()


class TestAWriteSavesOnlyWhatItDecided(AbstractTestAWriteSavesOnlyWhatItDecided):
    pass


class TestWhatAWriteSaves(AbstractTestWhatAWriteSaves):
    pass


async def test_a_one_to_one_held_from_the_other_side_moves_only_its_keys(writes):
    """``CwDoc.badge`` is held by the badge's key: giving doc 1 badge 2 clears
    badge 1's key and sets badge 2's, and leaves the rest of either row be."""
    docs = await seed(writes)
    await writes.create(CwBadge, id=2, label="silver")
    race = Race(writes, CwBadge, 1, "changed", {"label": "Theirs"})
    orm = racing_orm(writes, race)
    upsert = orm.mutations.upsert_node_input(
        model=CwDoc,
        project={
            "_meta": {
                "update": ["title"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            },
            "badge": {"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
        },
        name="CwDocBadgeUpsert",
    )
    badge = member(upsert, "update", "badge")
    target = member(badge, "upsert")
    link = badge(upsert=target(where=member(target, "where")(id="2")))
    await writes.upsert(orm, edit(upsert, docs.draft.id, badge=link))
    assert race.fired
    columns = ("label", "doc_id")
    assert await state(writes, CwBadge, 1, columns) == {
        "label": "Theirs",
        "doc_id": None,
    }
    assert await state(writes, CwBadge, 2, columns) == {"label": "silver", "doc_id": 1}
