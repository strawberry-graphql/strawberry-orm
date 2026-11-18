"""Upsert lookups through SQLAlchemy subclasses a discriminator reads back as themselves.

``WpSealedDoc`` has a table of its own; ``WpRestrictedDoc`` and ``WpPublicDoc``
share their base's.
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

from tests.abstract.write_lookup_scopes import AbstractTestWriteLookupScopes
from tests.backends.sqlalchemy.test_write_path_subclass_rows import SubclassWriteDb


class Base(DeclarativeBase):
    pass


class WpFolder(Base):
    __tablename__ = "wp_folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    docs: Mapped[list[WpDoc]] = relationship(
        back_populates="folder", order_by="WpDoc.id"
    )


class WpDoc(Base):
    __tablename__ = "wp_doc"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("wp_folder.id"))
    folder: Mapped[WpFolder | None] = relationship(back_populates="docs")


class WpSealedDoc(WpDoc):
    __tablename__ = "wp_sealed_doc"
    __mapper_args__ = {"polymorphic_identity": "sealed"}

    id: Mapped[int] = mapped_column(ForeignKey("wp_doc.id"), primary_key=True)


class WpRestrictedDoc(WpSealedDoc):
    __mapper_args__ = {"polymorphic_identity": "restricted"}


class WpPublicDoc(WpDoc):
    __mapper_args__ = {"polymorphic_identity": "public"}


def _bound(name: str, table: str) -> Table:
    return Table(
        f"wp_binder_{name}",
        Base.metadata,
        Column("binder_id", ForeignKey("wp_binder.id"), primary_key=True),
        Column("doc_id", ForeignKey(f"{table}.id"), primary_key=True),
    )


class WpBinder(Base):
    __tablename__ = "wp_binder"

    id: Mapped[int] = mapped_column(primary_key=True)
    docs: Mapped[list[WpDoc]] = relationship(secondary=_bound("docs", "wp_doc"))
    sealed: Mapped[list[WpSealedDoc]] = relationship(
        secondary=_bound("sealed", "wp_sealed_doc")
    )
    restricted: Mapped[list[WpRestrictedDoc]] = relationship(
        secondary=_bound("restricted", "wp_sealed_doc")
    )


class WpMark(Base):
    __tablename__ = "wp_mark"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("wp_doc.id"))
    sealed_id: Mapped[int | None] = mapped_column(ForeignKey("wp_sealed_doc.id"))
    restricted_id: Mapped[int | None] = mapped_column(ForeignKey("wp_sealed_doc.id"))
    doc: Mapped[WpDoc | None] = relationship(foreign_keys=[doc_id])
    sealed: Mapped[WpSealedDoc | None] = relationship(foreign_keys=[sealed_id])
    restricted: Mapped[WpRestrictedDoc | None] = relationship(
        foreign_keys=[restricted_id]
    )


MODELS = SimpleNamespace(
    Folder=WpFolder,
    Doc=WpDoc,
    SealedDoc=WpSealedDoc,
    RestrictedDoc=WpRestrictedDoc,
    PublicDoc=WpPublicDoc,
    Binder=WpBinder,
    Mark=WpMark,
)


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


class TestWriteLookupScopesWithADiscriminator(AbstractTestWriteLookupScopes):
    pass
