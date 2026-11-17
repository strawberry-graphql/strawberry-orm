"""Joined-table subclasses without ``polymorphic_on`` read through their base as base rows."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm import UpsertNotFoundError
from strawberry_orm.repo import _get_sa_pk_column
from tests.abstract.write_path_authorization import resolve
from tests.abstract.write_path_subclass_rows import (
    AbstractTestSubclassRowsReadAsTheBase,
    recorder,
)
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class WaFolder(Base):
    __tablename__ = "wa_folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Without a discriminator, holding a subclass row takes this.
    docs: Mapped[list[WaDoc]] = relationship(
        back_populates="folder", order_by="WaDoc.id", enable_typechecks=False
    )


class WaDoc(Base):
    __tablename__ = "wa_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("wa_folder.id"))
    folder: Mapped[WaFolder | None] = relationship(back_populates="docs")


class WaSealedDoc(WaDoc):
    __tablename__ = "wa_sealed_doc"

    id: Mapped[int] = mapped_column(ForeignKey("wa_doc.id"), primary_key=True)
    note: Mapped[str] = mapped_column(String(50), default="")


class WaRestrictedDoc(WaSealedDoc):
    __tablename__ = "wa_restricted_doc"

    id: Mapped[int] = mapped_column(ForeignKey("wa_sealed_doc.id"), primary_key=True)


class WaPublicDoc(WaDoc):
    __tablename__ = "wa_public_doc"

    id: Mapped[int] = mapped_column(ForeignKey("wa_doc.id"), primary_key=True)


class WaPin(Base):
    __tablename__ = "wa_pin"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("wa_doc.id"))
    doc: Mapped[WaDoc | None] = relationship(enable_typechecks=False)


class WaNote(Base):
    __tablename__ = "wa_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class WaSealedNote(WaNote):
    __tablename__ = "wa_sealed_note"

    note_id: Mapped[int] = mapped_column(ForeignKey("wa_note.id"), primary_key=True)


MODELS = SimpleNamespace(
    Folder=WaFolder,
    Doc=WaDoc,
    SealedDoc=WaSealedDoc,
    RestrictedDoc=WaRestrictedDoc,
    PublicDoc=WaPublicDoc,
    Pin=WaPin,
)


@dataclass
class SubclassWriteDb(SQLAlchemyWriteDb):
    async def also_a(self, model: type, pk: int) -> None:
        await resolve(self.session.execute(insert(model.__table__).values(id=pk)))
        await resolve(self.session.commit())

    async def rows(self, model: type) -> list[Any]:
        # A row read as its base and as its subclass is two objects in the
        # session; a write through one leaves the other as it was read.
        statement = (
            select(model)
            .order_by(_get_sa_pk_column(model))
            .execution_options(populate_existing=True)
        )
        result = await resolve(self.session.execute(statement))
        return list(result.scalars().all())


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SubclassWriteDb(MODELS, async_hooks=False, session=session)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SubclassWriteDb(MODELS, async_hooks=True, session=session)
    await engine.dispose()


class TestSubclassRowsReadAsTheBase(AbstractTestSubclassRowsReadAsTheBase):
    pass


async def test_a_subclass_key_named_apart_from_the_base_key_holds_its_rows_only(
    writes,
):
    """``WaSealedNote.id`` is ``wa_note.id`` alone; read off it, every note
    would be a sealed note, which the caller cannot see."""
    await writes.create(WaNote, id=1, title="plain")
    await writes.create(WaSealedNote, id=2, title="sealed")
    calls = []
    orm = writes.new_orm(
        repos={
            WaNote: recorder(writes, calls, "note", None),
            WaSealedNote: recorder(writes, calls, "sealed", []),
        }
    )
    by_id, by_title = (
        orm.mutations.upsert_node_input(
            model=WaNote,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": [field], "onMatch": "UPDATE"},
                }
            },
            name=f"WaNoteBy{field.title()}",
        )
        for field in ("id", "title")
    )
    edit = AbstractTestSubclassRowsReadAsTheBase.edit
    await writes.upsert(orm, edit(by_id, "edited", id="1"))
    await writes.upsert(orm, edit(by_title, "renamed", title="edited"))
    with pytest.raises(UpsertNotFoundError):
        await writes.upsert(orm, edit(by_id, "pwned", id="2"))
    assert calls == [("note", "can_update", 1), ("note", "can_update", 1)]
    notes = await writes.rows(WaNote)
    assert [note.title for note in notes] == ["renamed", "sealed"]
