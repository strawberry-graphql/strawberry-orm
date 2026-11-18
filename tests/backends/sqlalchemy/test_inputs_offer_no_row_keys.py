from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm.repo import _get_sa_pk_column
from tests.abstract.inputs_offer_no_row_keys import (
    ME,
    THEM,
    AbstractTestACreateOnlyInserts,
    AbstractTestALinkKeepsItsRow,
    AbstractTestAnUpdateKeepsItsRow,
    AbstractTestInputsOfferNoRowKeys,
    KeyedDb,
    Shape,
)
from tests.abstract.write_path_authorization import resolve
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class WkBinder(Base):
    __tablename__ = "wk_binder"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[int] = mapped_column(default=0)
    contracts: Mapped[list[WkContract]] = relationship(
        foreign_keys="WkContract.holder_id"
    )
    sealeds: Mapped[list[WkSealed]] = relationship(foreign_keys="WkSealed.keeper_id")
    memos: Mapped[list[WkMemo]] = relationship(foreign_keys="WkMemo.shelf_id")
    plains: Mapped[list[WkPlain]] = relationship(foreign_keys="WkPlain.board_id")
    sealed_notes: Mapped[list[WkSealedNote]] = relationship(
        foreign_keys="WkSealedNote.book_id"
    )
    cards: Mapped[list[WkCard]] = relationship()
    tags: Mapped[list[WkTag]] = relationship()
    slots: Mapped[list[WkSlot]] = relationship()


class WkDoc(Base):
    __tablename__ = "wk_doc"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    kind: Mapped[str] = mapped_column(String(20))


class WkContract(WkDoc):
    __tablename__ = "wk_contract"
    __mapper_args__ = {"polymorphic_identity": "contract"}

    id: Mapped[int] = mapped_column(ForeignKey("wk_doc.id"), primary_key=True)
    terms: Mapped[str] = mapped_column(String(50), default="")
    holder_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkSealed(WkContract):
    __tablename__ = "wk_sealed"
    __mapper_args__ = {"polymorphic_identity": "sealed"}

    id: Mapped[int] = mapped_column(ForeignKey("wk_contract.id"), primary_key=True)
    keeper_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkMemo(WkDoc):
    __mapper_args__ = {"polymorphic_identity": "memo"}

    shelf_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkNote(Base):
    __tablename__ = "wk_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)


class WkPlain(WkNote):
    __tablename__ = "wk_plain"

    id: Mapped[int] = mapped_column(ForeignKey("wk_note.id"), primary_key=True)
    board_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkSealedNote(WkNote):
    __tablename__ = "wk_sealed_note"

    note_id: Mapped[int] = mapped_column(ForeignKey("wk_note.id"), primary_key=True)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkCard(WkNote):
    __tablename__ = "wk_card"
    __mapper_args__ = {"concrete": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    binder_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkStamped(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)


class WkTag(WkStamped):
    __tablename__ = "wk_tag"

    binder_id: Mapped[int | None] = mapped_column(ForeignKey("wk_binder.id"))


class WkBadge(Base):
    __tablename__ = "wk_badge"

    binder_id: Mapped[int] = mapped_column(ForeignKey("wk_binder.id"), primary_key=True)
    binder: Mapped[WkBinder] = relationship()
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)


class WkSlot(Base):
    __tablename__ = "wk_slot"

    binder_id: Mapped[int] = mapped_column(ForeignKey("wk_binder.id"), primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)


class WkPin(Base):
    __tablename__ = "wk_pin"

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("wk_contract.id"))
    contract: Mapped[WkContract | None] = relationship(foreign_keys="WkPin.contract_id")
    sealed_id: Mapped[int | None] = mapped_column(ForeignKey("wk_sealed.id"))
    sealed: Mapped[WkSealed | None] = relationship(foreign_keys="WkPin.sealed_id")
    memo_id: Mapped[int | None] = mapped_column(ForeignKey("wk_doc.id"))
    memo: Mapped[WkMemo | None] = relationship(foreign_keys="WkPin.memo_id")
    plain_id: Mapped[int | None] = mapped_column(ForeignKey("wk_plain.id"))
    plain: Mapped[WkPlain | None] = relationship(foreign_keys="WkPin.plain_id")
    sealed_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("wk_sealed_note.note_id")
    )
    sealed_note: Mapped[WkSealedNote | None] = relationship(
        foreign_keys="WkPin.sealed_note_id"
    )
    card_id: Mapped[int | None] = mapped_column(ForeignKey("wk_card.id"))
    card: Mapped[WkCard | None] = relationship(foreign_keys="WkPin.card_id")
    tag_id: Mapped[int | None] = mapped_column(ForeignKey("wk_tag.id"))
    tag: Mapped[WkTag | None] = relationship(foreign_keys="WkPin.tag_id")
    badge_id: Mapped[int | None] = mapped_column(ForeignKey("wk_badge.binder_id"))
    badge: Mapped[WkBadge | None] = relationship(foreign_keys="WkPin.badge_id")


SHAPES = {
    "joined": Shape(
        WkContract, {"id"}, ("id",), WkDoc, relation="contracts", link="contract"
    ),
    "multi_level_joined": Shape(
        WkSealed, {"id"}, ("id",), WkContract, relation="sealeds", link="sealed"
    ),
    "single_table": Shape(
        WkMemo, {"id"}, ("id",), WkDoc, relation="memos", link="memo"
    ),
    "joined_without_discriminator": Shape(
        WkPlain, {"id"}, ("id",), WkNote, relation="plains", link="plain"
    ),
    "joined_named_key": Shape(
        WkSealedNote,
        {"id", "note_id"},
        ("id", "note_id"),
        WkNote,
        relation="sealed_notes",
        link="sealed_note",
    ),
    "concrete": Shape(WkCard, {"id"}, ("id",), WkCard, relation="cards", link="card"),
    "abstract_base": Shape(WkTag, {"id"}, ("id",), WkTag, relation="tags", link="tag"),
    "one_to_one_key": Shape(
        WkBadge,
        {"binder", "binder_id"},
        ("binder_id",),
        WkBadge,
        victim_key="binder_id",
        link="badge",
    ),
}


MODELS = SimpleNamespace(Binder=WkBinder, Pin=WkPin, Slot=WkSlot)


@dataclass
class SQLAlchemyKeyedDb(KeyedDb, SQLAlchemyWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.where(model.owner == ME)

    async def rows(self, model: type) -> list[Any]:
        statement = (
            select(model)
            .order_by(_get_sa_pk_column(model))
            .execution_options(populate_existing=True)
        )
        result = await resolve(self.session.execute(statement))
        return list(result.scalars().all())


async def _binders(keyed: SQLAlchemyKeyedDb) -> SQLAlchemyKeyedDb:
    for pk in (1, 2, 3):
        await keyed.create(WkBinder, id=pk, owner=THEM)
    return keyed


@pytest_asyncio.fixture(params=["sync", "async"])
async def keyed(request):
    db = {"m": MODELS, "shapes": SHAPES}
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield await _binders(
            SQLAlchemyKeyedDb(async_hooks=False, session=session, **db)
        )
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield await _binders(SQLAlchemyKeyedDb(async_hooks=True, session=session, **db))
    await engine.dispose()


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoRowKeys(AbstractTestInputsOfferNoRowKeys):
    pass


class TestACreateOnlyInserts(AbstractTestACreateOnlyInserts):
    pass


class TestAnUpdateKeepsItsRow(AbstractTestAnUpdateKeepsItsRow):
    pass


class TestALinkKeepsItsRow(AbstractTestALinkKeepsItsRow):
    pass
