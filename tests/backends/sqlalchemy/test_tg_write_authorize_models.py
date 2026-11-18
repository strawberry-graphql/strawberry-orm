"""A polymorphic subclass reads back as itself; a joined-table one without a
discriminator reads back as its base."""

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
from tests.abstract.tg_write_authorize_models import (
    AbstractTestEveryModelOfTheRowIsAsked,
)
from tests.abstract.write_path_authorization import resolve
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class TgShelf(Base):
    __tablename__ = "tg_shelf"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    docs: Mapped[list[TgDoc]] = relationship(
        back_populates="shelf", order_by="TgDoc.id"
    )


class TgDoc(Base):
    __tablename__ = "tg_doc"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))
    shelf_id: Mapped[int | None] = mapped_column(ForeignKey("tg_shelf.id"))
    shelf: Mapped[TgShelf | None] = relationship(back_populates="docs")


class TgContract(TgDoc):
    __mapper_args__ = {"polymorphic_identity": "contract"}


class TgPin(Base):
    __tablename__ = "tg_pin"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("tg_doc.id"))
    doc: Mapped[TgDoc | None] = relationship()


class TgjShelf(Base):
    __tablename__ = "tg_j_shelf"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    docs: Mapped[list[TgjDoc]] = relationship(
        back_populates="shelf", order_by="TgjDoc.id", enable_typechecks=False
    )


class TgjDoc(Base):
    __tablename__ = "tg_j_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    shelf_id: Mapped[int | None] = mapped_column(ForeignKey("tg_j_shelf.id"))
    shelf: Mapped[TgjShelf | None] = relationship(back_populates="docs")


class TgjContract(TgjDoc):
    __tablename__ = "tg_j_contract"

    id: Mapped[int] = mapped_column(ForeignKey("tg_j_doc.id"), primary_key=True)


class TgjPin(Base):
    __tablename__ = "tg_j_pin"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("tg_j_doc.id"))
    doc: Mapped[TgjDoc | None] = relationship(enable_typechecks=False)


SHAPES = {
    "polymorphic": SimpleNamespace(
        Shelf=TgShelf, Doc=TgDoc, Contract=TgContract, Pin=TgPin
    ),
    "joined": SimpleNamespace(
        Shelf=TgjShelf, Doc=TgjDoc, Contract=TgjContract, Pin=TgjPin
    ),
}


@dataclass
class TgWriteDb(SQLAlchemyWriteDb):
    async def rows(self, model: type) -> list[Any]:
        statement = (
            select(model)
            .order_by(_get_sa_pk_column(model))
            .execution_options(populate_existing=True)
        )
        result = await resolve(self.session.execute(statement))
        return list(result.scalars().all())


@pytest_asyncio.fixture(
    params=[
        ("polymorphic", "sync"),
        ("polymorphic", "async"),
        ("joined", "sync"),
        ("joined", "async"),
    ],
    ids="-".join,
)
async def writes(request):
    shape, mode = request.param
    models = SHAPES[shape]
    if mode == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield TgWriteDb(models, async_hooks=False, session=session)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield TgWriteDb(models, async_hooks=True, session=session)
    await engine.dispose()


class TestEveryModelOfTheRowIsAsked(AbstractTestEveryModelOfTheRowIsAsked):
    pass
