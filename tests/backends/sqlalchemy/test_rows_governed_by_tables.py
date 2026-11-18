"""Each way SQLAlchemy maps a subclass: in its base's table, or in one of its own."""

from dataclasses import dataclass
from inspect import isawaitable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Column, ForeignKey, Integer, String, Table, create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.declarative import AbstractConcreteBase, ConcreteBase
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from strawberry_orm.repo import _get_sa_pk_column
from tests.abstract.rows_governed_by_tables import (
    AbstractTestRowsGovernedByTables,
    Shape,
)
from tests.abstract.write_path_authorization import resolve
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class GjDoc(Base):
    __tablename__ = "gj_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GjSub(GjDoc):
    __tablename__ = "gj_sub"

    id: Mapped[int] = mapped_column(ForeignKey("gj_doc.id"), primary_key=True)


class GsDoc(Base):
    __tablename__ = "gs_doc"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(10))


class GsSub(GsDoc):
    __mapper_args__ = {"polymorphic_identity": "sub"}


class GcDoc(Base):
    __tablename__ = "gc_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GcSub(GcDoc):
    __tablename__ = "gc_sub"
    __mapper_args__ = {"concrete": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GuDoc(ConcreteBase, Base):
    __tablename__ = "gu_doc"
    __mapper_args__ = {"polymorphic_identity": "doc", "concrete": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GuSub(GuDoc):
    __tablename__ = "gu_sub"
    __mapper_args__ = {"polymorphic_identity": "sub", "concrete": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GxDoc(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


class GxSub(GxDoc):
    __tablename__ = "gx_sub"


class Apart(DeclarativeBase):
    """A registry holding an abstract concrete base, whose union is no table."""


class GaDoc(AbstractConcreteBase, Apart):
    strict_attrs = True


class GaSub(GaDoc):
    __tablename__ = "ga_sub"
    __mapper_args__ = {"polymorphic_identity": "sub", "concrete": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))


def holders(prefix: str, sub: type, key: str) -> SimpleNamespace:
    """A shelf listing *sub* rows and a pin linking one, by the column *key*."""
    registry = next(base for base in (Base, Apart) if issubclass(sub, base))
    listing = Table(
        f"{prefix}_shelf_doc",
        registry.metadata,
        Column("shelf_id", ForeignKey(f"{prefix}_shelf.id"), primary_key=True),
        Column("doc_id", ForeignKey(key), primary_key=True),
    )
    name = prefix.title()
    shelf = type(
        f"{name}Shelf",
        (registry,),
        {
            "__tablename__": f"{prefix}_shelf",
            "id": mapped_column(Integer, primary_key=True),
            "name": mapped_column(String(50)),
            "docs": relationship(sub, secondary=listing, order_by=sub.id),
        },
    )
    pin = type(
        f"{name}Pin",
        (registry,),
        {
            "__tablename__": f"{prefix}_pin",
            "id": mapped_column(Integer, primary_key=True),
            "label": mapped_column(String(50)),
            "doc_id": mapped_column(ForeignKey(key), nullable=True),
            "doc": relationship(sub),
        },
    )
    return SimpleNamespace(Shelf=shelf, Pin=pin)


def models(prefix: str, base: type, sub: type, key: str) -> SimpleNamespace:
    return SimpleNamespace(Base=base, Sub=sub, **vars(holders(prefix, sub, key)))


SHAPES = {
    "joined": (models("gj", GjDoc, GjSub, "gj_sub.id"), Shape(True, True)),
    "single table": (models("gs", GsDoc, GsSub, "gs_doc.id"), Shape(True, True)),
    "concrete": (
        models("gc", GcDoc, GcSub, "gc_sub.id"),
        Shape(False, False, apart=True),
    ),
    # No type over a mapping in a polymorphic union can be built: the union's
    # columns say nothing of nulls.
    "concrete union": (
        models("gu", GuDoc, GuSub, "gu_sub.id"),
        Shape(False, False, apart=True, typed=False, readable=False),
    ),
    "abstract concrete base": (
        models("ga", GaDoc, GaSub, "ga_sub.id"),
        Shape(False, False, typed=False),
    ),
    "abstract base": (
        models("gx", GxDoc, GxSub, "gx_sub.id"),
        Shape(False, True, typed=False),
    ),
}


@dataclass
class GovernedWriteDb(SQLAlchemyWriteDb):
    shape: Shape | None = None

    def only(self, query: Any, model: type, pks: list[int]) -> Any:
        # A class mapping no table has no columns: a scope over it names
        # those of the statement it is given.
        if sa_inspect(model, raiseerr=False) is None:
            model = query.column_descriptions[0]["entity"]
        return super().only(query, model, pks)

    def docs(self) -> Any:
        return select(self.m.Sub).order_by(_get_sa_pk_column(self.m.Sub))

    def shelves(self) -> Any:
        return select(self.m.Shelf).order_by(self.m.Shelf.id)

    def read(self, query: Any) -> Any:
        rows = self.session.scalars(query)
        if isawaitable(rows):

            async def listed() -> list[Any]:
                return list(await rows)

            return listed()
        return list(rows)

    async def rows(self, model: type) -> list[Any]:
        statement = (
            select(model)
            .order_by(_get_sa_pk_column(model))
            .execution_options(populate_existing=True)
        )
        result = await resolve(self.session.execute(statement))
        return list(result.scalars().all())


@pytest_asyncio.fixture(
    params=[(shape, mode) for shape in SHAPES for mode in ("sync", "async")],
    ids=lambda param: f"{param[0]}-{param[1]}",
)
async def writes(request):
    name, mode = request.param
    shaped, shape = SHAPES[name]
    if mode == "sync":
        engine = create_engine("sqlite:///:memory:")
        for registry in (Base, Apart):
            registry.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield GovernedWriteDb(shaped, async_hooks=False, session=session, shape=shape)
        session.close()
        return
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for registry in (Base, Apart):
            await conn.run_sync(registry.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield GovernedWriteDb(shaped, async_hooks=True, session=session, shape=shape)
    await engine.dispose()


class TestRowsGovernedByTables(AbstractTestRowsGovernedByTables):
    pass
