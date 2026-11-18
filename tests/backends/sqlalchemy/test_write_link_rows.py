"""Members go through ``lr_membership``, which ``LrMembership`` maps; fans
through a plain ``Table``.

The link rows' foreign keys declare no ``ON DELETE CASCADE``: the ORM deletes
a deleted person's link rows itself, through ``LrPerson.clubs``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import Column, ForeignKey, String, Table, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.write_link_rows import AbstractTestLinkRowsAreWritesOfTheirModel
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


fans = Table(
    "lr_fan",
    Base.metadata,
    Column("club_id", ForeignKey("lr_club.id"), primary_key=True),
    Column("person_id", ForeignKey("lr_person.id"), primary_key=True),
)


class LrPerson(Base):
    __tablename__ = "lr_person"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    clubs: Mapped[list[LrClub]] = relationship(
        secondary="lr_membership", back_populates="members"
    )
    fan_of: Mapped[list[LrClub]] = relationship(secondary=fans, back_populates="fans")


class LrClub(Base):
    __tablename__ = "lr_club"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    members: Mapped[list[LrPerson]] = relationship(
        secondary="lr_membership", back_populates="clubs"
    )
    fans: Mapped[list[LrPerson]] = relationship(secondary=fans, back_populates="fan_of")


class LrMembership(Base):
    __tablename__ = "lr_membership"

    id: Mapped[int] = mapped_column(primary_key=True)
    club_id: Mapped[int] = mapped_column(ForeignKey("lr_club.id"))
    person_id: Mapped[int] = mapped_column(ForeignKey("lr_person.id"))


MODELS = SimpleNamespace(Person=LrPerson, Club=LrClub, Membership=LrMembership)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SQLAlchemyWriteDb(MODELS, async_hooks=False, session=session)
        session.close()
        return
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SQLAlchemyWriteDb(MODELS, async_hooks=True, session=session)
    await engine.dispose()


class TestLinkRowsAreWritesOfTheirModel(AbstractTestLinkRowsAreWritesOfTheirModel):
    pass
