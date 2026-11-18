"""A box's notes go with it through ``cascade="all"``, ``"all, delete-orphan"``,
``passive_deletes`` over ``ON DELETE CASCADE``, or the foreign key alone; a
sealed note is a joined-table subclass without a discriminator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.tg_delete_cascades import AbstractTestADeleteAsksTheRowsItCascadesTo
from tests.backends.sqlalchemy.test_tg_write_authorize_models import TgWriteDb


def _shape(style: str) -> SimpleNamespace:
    """Shelf, Box, Note, Stamp and Pin, a box's notes and a note's stamps going with it per *style*."""
    on_db = style in ("passive", "database")
    down: dict[str, Any] = {
        "cascade": {"cascade": "all"},
        "delete-orphan": {"cascade": "all, delete-orphan"},
        "passive": {"passive_deletes": True},
    }.get(style, {})
    up = {"ondelete": "CASCADE"} if on_db else {}

    class Base(DeclarativeBase):
        pass

    class TgcShelf(Base):
        __tablename__ = "tgc_shelf"

        id: Mapped[int] = mapped_column(primary_key=True)
        label: Mapped[str] = mapped_column(String(50))
        boxes: Mapped[list[TgcBox]] = relationship(
            back_populates="shelf", order_by="TgcBox.id"
        )

    class TgcBox(Base):
        __tablename__ = "tgc_box"

        id: Mapped[int] = mapped_column(primary_key=True)
        label: Mapped[str] = mapped_column(String(50))
        shelf_id: Mapped[int | None] = mapped_column(ForeignKey("tgc_shelf.id"))
        shelf: Mapped[TgcShelf | None] = relationship(back_populates="boxes")
        if style != "database":
            notes: Mapped[list[TgcNote]] = relationship(**down)

    class TgcNote(Base):
        __tablename__ = "tgc_note"

        id: Mapped[int] = mapped_column(primary_key=True)
        body: Mapped[str] = mapped_column(String(50))
        box_id: Mapped[int] = mapped_column(ForeignKey("tgc_box.id", **up))
        if style == "database":
            box: Mapped[TgcBox] = relationship()
        else:
            stamps: Mapped[list[TgcStamp]] = relationship(**down)

    class TgcSealedNote(TgcNote):
        __tablename__ = "tgc_sealed_note"

        id: Mapped[int] = mapped_column(
            ForeignKey("tgc_note.id", **up), primary_key=True
        )

    class TgcStamp(Base):
        __tablename__ = "tgc_stamp"

        id: Mapped[int] = mapped_column(primary_key=True)
        mark: Mapped[str] = mapped_column(String(50))
        note_id: Mapped[int] = mapped_column(ForeignKey("tgc_note.id", **up))

    orphans = (
        {"cascade": "all, delete-orphan", "single_parent": True}
        if style == "delete-orphan"
        else {}
    )

    class TgcPin(Base):
        __tablename__ = "tgc_pin"

        id: Mapped[int] = mapped_column(primary_key=True)
        label: Mapped[str] = mapped_column(String(50))
        box_id: Mapped[int | None] = mapped_column(
            ForeignKey("tgc_box.id", ondelete="SET NULL")
        )
        box: Mapped[TgcBox | None] = relationship(**orphans)

    return SimpleNamespace(
        Base=Base,
        Shelf=TgcShelf,
        Box=TgcBox,
        Note=TgcNote,
        SealedNote=TgcSealedNote,
        Stamp=TgcStamp,
        Pin=TgcPin,
    )


STYLES = ["cascade", "delete-orphan", "passive", "database"]
SHAPES = {style: _shape(style) for style in STYLES}


def _enforce_foreign_keys(engine: Any) -> None:
    @event.listens_for(engine, "connect")
    def _on(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")


@pytest_asyncio.fixture(
    params=[(style, mode) for style in STYLES for mode in ("sync", "async")],
    ids="-".join,
)
async def writes(request):
    style, mode = request.param
    models = SHAPES[style]
    if mode == "sync":
        engine = create_engine("sqlite:///:memory:")
        _enforce_foreign_keys(engine)
        models.Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield TgWriteDb(models, async_hooks=False, session=session)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enforce_foreign_keys(engine.sync_engine)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield TgWriteDb(models, async_hooks=True, session=session)
    await engine.dispose()


class TestADeleteAsksTheRowsItCascadesTo(AbstractTestADeleteAsksTheRowsItCascadesTo):
    pass
