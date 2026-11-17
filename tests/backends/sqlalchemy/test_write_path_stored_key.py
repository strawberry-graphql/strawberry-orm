"""Files keyed by text SQLite compares with ``COLLATE NOCASE``, and the errors
writing them raises."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.write_path_error_names import AbstractTestErrorsNameTheClientsType
from tests.abstract.write_path_stored_key import AbstractTestAuthorizeIsToldTheStoredKey
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class WaShelf(Base):
    __tablename__ = "wa_shelf"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    files: Mapped[list[WaFile]] = relationship(back_populates="shelf")


class WaFile(Base):
    __tablename__ = "wa_file"

    id: Mapped[str] = mapped_column(String(20, collation="NOCASE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    shelf_id: Mapped[int | None] = mapped_column(ForeignKey("wa_shelf.id"))
    shelf: Mapped[WaShelf | None] = relationship(back_populates="files")


class WaTag(Base):
    __tablename__ = "wa_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    file_id: Mapped[str | None] = mapped_column(ForeignKey("wa_file.id"))
    file: Mapped[WaFile | None] = relationship()


MODELS = SimpleNamespace(Shelf=WaShelf, File=WaFile, Tag=WaTag)


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


class TestAuthorizeIsToldTheStoredKey(AbstractTestAuthorizeIsToldTheStoredKey):
    pass


class TestErrorsNameTheClientsType(AbstractTestErrorsNameTheClientsType):
    pass
