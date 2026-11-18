from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    String,
    case,
    create_engine,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    column_property,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.write_path_authorization import resolve
from tests.abstract.write_path_identity_columns import (
    ME,
    AbstractTestACreateKeepsItsModel,
    AbstractTestALinkKeepsItsRowAndModel,
    AbstractTestAnUpdateKeepsItsRowAndModel,
    AbstractTestInputsOfferNoIdentityColumn,
    Held,
    IdentityDb,
    Shape,
)
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


def _holder() -> Any:
    return mapped_column(ForeignKey("ic_holder.id"), nullable=True)


class IcHolder(Base):
    __tablename__ = "ic_holder"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[int] = mapped_column(default=0)
    notes: Mapped[list[IcNote]] = relationship()
    badges: Mapped[list[IcBadge]] = relationship()
    slots: Mapped[list[IcSlot]] = relationship()
    seats: Mapped[list[IcSeat]] = relationship()
    docs: Mapped[list[IcDoc]] = relationship()
    contracts: Mapped[list[IcContract]] = relationship()
    emps: Mapped[list[IcEmp]] = relationship()
    things: Mapped[list[IcThing]] = relationship()
    items: Mapped[list[IcItem]] = relationship()


class IcTenant(Base):
    __tablename__ = "ic_tenant"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[int] = mapped_column(default=0)
    slots: Mapped[list[IcSlot]] = relationship(back_populates="tenant")
    seats: Mapped[list[IcSeat]] = relationship(back_populates="tenant")


class IcNote(Base):
    __tablename__ = "ic_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    holder_id: Mapped[int | None] = _holder()


class IcBadge(Base):
    __tablename__ = "ic_badge"

    tenant: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    holder_id: Mapped[int | None] = _holder()


class IcSlot(Base):
    __tablename__ = "ic_slot"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("ic_tenant.id"), primary_key=True)
    tenant: Mapped[IcTenant] = relationship(back_populates="slots")
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    holder_id: Mapped[int | None] = _holder()


class IcSeat(Base):
    __tablename__ = "ic_seat"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("ic_tenant.id"), primary_key=True)
    tenant: Mapped[IcTenant] = relationship(back_populates="seats")
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    holder_id: Mapped[int | None] = _holder()


class IcDoc(Base):
    __tablename__ = "ic_doc"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    kind: Mapped[str] = mapped_column(String(20))
    holder_id: Mapped[int | None] = _holder()


class IcSealedDoc(IcDoc):
    __mapper_args__ = {"polymorphic_identity": "sealed"}


class IcFile(Base):
    __tablename__ = "ic_file"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "file"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    kind: Mapped[str] = mapped_column(String(20))


class IcContract(IcFile):
    __tablename__ = "ic_contract"
    __mapper_args__ = {"polymorphic_identity": "contract"}

    id: Mapped[int] = mapped_column(ForeignKey("ic_file.id"), primary_key=True)
    holder_id: Mapped[int | None] = _holder()


class IcRestricted(IcContract):
    __tablename__ = "ic_restricted"
    __mapper_args__ = {"polymorphic_identity": "restricted"}

    id: Mapped[int] = mapped_column(ForeignKey("ic_contract.id"), primary_key=True)


class IcEmp(Base):
    """Typed by what an expression makes of ``code``."""

    __tablename__ = "ic_emp"

    id = Column(Integer, primary_key=True)
    title = Column(String(50), nullable=False)
    owner = Column(Integer, default=0)
    code = Column(String(20), default="")
    holder_id = Column(ForeignKey("ic_holder.id"), nullable=True)
    __mapper_args__ = {
        "polymorphic_on": case((code == "eng", "engineer"), else_="emp"),
        "polymorphic_identity": "emp",
    }


class IcEngineer(IcEmp):
    __mapper_args__ = {"polymorphic_identity": "engineer"}


class IcThing(Base):
    """Typed by a mapped expression over ``grade``."""

    __tablename__ = "ic_thing"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    grade: Mapped[int] = mapped_column(default=0)
    holder_id: Mapped[int | None] = _holder()
    rank = column_property(case((grade > 5, "rare"), else_="thing"))
    __mapper_args__ = {"polymorphic_on": rank, "polymorphic_identity": "thing"}


class IcRareThing(IcThing):
    __mapper_args__ = {"polymorphic_identity": "rare"}


class IcKind(Base):
    __tablename__ = "ic_kind"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner: Mapped[int] = mapped_column(default=0)
    items: Mapped[list[IcItem]] = relationship(back_populates="kind")


class IcItem(Base):
    """Typed by the row its foreign key links."""

    __tablename__ = "ic_item"
    __mapper_args__ = {"polymorphic_on": "kind_id", "polymorphic_identity": 1}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    owner: Mapped[int] = mapped_column(default=0)
    kind_id: Mapped[int | None] = mapped_column(ForeignKey("ic_kind.id"))
    kind: Mapped[IcKind | None] = relationship(back_populates="items")
    holder_id: Mapped[int | None] = _holder()


class IcSpecial(IcItem):
    __mapper_args__ = {"polymorphic_identity": 2}


class IcPin(Base):
    __tablename__ = "ic_pin"

    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("ic_note.id"))
    note: Mapped[IcNote | None] = relationship()
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("ic_seat.tenant_id"))
    seat: Mapped[IcSeat | None] = relationship()
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("ic_doc.id"))
    doc: Mapped[IcDoc | None] = relationship()
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("ic_contract.id"))
    contract: Mapped[IcContract | None] = relationship()
    emp_id: Mapped[int | None] = mapped_column(ForeignKey("ic_emp.id"))
    emp: Mapped[IcEmp | None] = relationship()
    thing_id: Mapped[int | None] = mapped_column(ForeignKey("ic_thing.id"))
    thing: Mapped[IcThing | None] = relationship()
    item_id: Mapped[int | None] = mapped_column(ForeignKey("ic_item.id"))
    item: Mapped[IcItem | None] = relationship()


SHAPES = {
    "key": Shape(
        IcNote,
        {"id"},
        mine={"id": 1},
        theirs={"id": 2},
        moves=[{"id": 2}, {"id": 50}],
        forges=[{"id": 2}],
        columns=("id",),
        own_id="1",
        relation="notes",
        link="note",
    ),
    "composite": Shape(
        IcBadge,
        {"tenant", "code"},
        mine={"tenant": 1, "code": "a"},
        theirs={"tenant": 2, "code": "a"},
        moves=[{"tenant": 2}, {"code": "z"}],
        forges=[{"tenant": 2, "code": "a"}],
        columns=("tenant", "code"),
        relation="badges",
        keyed_by_hook={"tenant": 5, "code": "n"},
    ),
    "composite_foreign_key": Shape(
        IcSlot,
        {"tenant", "tenant_id", "code"},
        mine={"tenant_id": 1, "code": "a"},
        theirs={"tenant_id": 2, "code": "a"},
        moves=[{"tenant_id": 2}, {"code": "z"}],
        forges=[{"tenant_id": 2, "code": "a"}],
        columns=("tenant_id", "code"),
        relation="slots",
        held_by=Held(IcTenant, "slots", mine=1, other=3),
        keyed_by_hook={"tenant_id": 3, "code": "n"},
    ),
    "foreign_key_key": Shape(
        IcSeat,
        {"tenant", "tenant_id"},
        mine={"tenant_id": 1},
        theirs={"tenant_id": 2},
        moves=[{"tenant_id": 2}, {"tenant_id": 3}],
        forges=[{"tenant_id": 2}],
        columns=("tenant_id",),
        own_id="1",
        relation="seats",
        link="seat",
        held_by=Held(IcTenant, "seats", mine=1, other=3),
        keyed_by_hook={"tenant_id": 3},
    ),
    "discriminator": Shape(
        IcDoc,
        {"id", "kind"},
        mine={"id": 1},
        theirs={"id": 2},
        moves=[{"kind": "sealed"}],
        forges=[{"kind": "sealed"}],
        columns=("id", "kind"),
        picks_model={"kind"},
        own_id="1",
        sealed=(IcSealedDoc,),
        sealed_sets_its_own=True,
        relation="docs",
        link="doc",
    ),
    "joined_discriminator": Shape(
        IcContract,
        {"id", "kind"},
        mine={"id": 1},
        theirs={"id": 2},
        moves=[{"kind": "restricted"}, {"kind": "file"}],
        forges=[{"kind": "restricted"}, {"kind": "file"}],
        columns=("id", "kind"),
        picks_model={"kind"},
        own_id="1",
        base=IcFile,
        sealed=(IcRestricted,),
        sealed_sets_its_own=True,
        relation="contracts",
        link="contract",
    ),
    "expression_discriminator": Shape(
        IcEmp,
        {"id", "code", "_sa_polymorphic_on"},
        mine={"id": 1, "code": "x"},
        theirs={"id": 2, "code": "y"},
        moves=[{"code": "eng"}],
        forges=[{"code": "eng"}],
        columns=("id", "code"),
        picks_model={"code", "_sa_polymorphic_on"},
        own_id="1",
        sealed=(IcEngineer,),
        relation="emps",
        link="emp",
    ),
    "column_property_discriminator": Shape(
        IcThing,
        {"id", "grade", "rank"},
        mine={"id": 1, "grade": 1},
        theirs={"id": 2, "grade": 2},
        moves=[{"grade": 9}],
        forges=[{"grade": 9}],
        columns=("id", "grade"),
        picks_model={"grade", "rank"},
        own_id="1",
        sealed=(IcRareThing,),
        relation="things",
        link="thing",
    ),
    "foreign_key_discriminator": Shape(
        IcItem,
        {"id", "kind", "kind_id"},
        mine={"id": 1, "kind_id": 1},
        theirs={"id": 2, "kind_id": 1},
        moves=[{"kind_id": 2}],
        forges=[{"kind_id": 2}],
        columns=("id", "kind_id"),
        picks_model={"kind", "kind_id"},
        own_id="1",
        sealed=(IcSpecial,),
        sealed_sets_its_own=True,
        relation="items",
        link="item",
        held_by=Held(IcKind, "items", mine=1, other=2, picks_model=True),
    ),
}


MODELS = SimpleNamespace(Holder=IcHolder, Pin=IcPin)


@dataclass
class SQLAlchemyIdentityDb(IdentityDb, SQLAlchemyWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.where(model.owner == ME)

    async def fetch(self, model: type, pk: Any) -> Any:
        return await resolve(self.session.get(model, pk))

    async def rows(self, model: type) -> list[Any]:
        # Loaded again, a row whose discriminator changed is of its new model.
        self.session.expunge_all()
        result = await resolve(self.session.execute(select(model)))
        return list(result.scalars().all())


@pytest_asyncio.fixture(params=["sync", "async"])
async def identity(request):
    db = {"m": MODELS, "shapes": SHAPES}
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SQLAlchemyIdentityDb(async_hooks=False, session=session, **db)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SQLAlchemyIdentityDb(async_hooks=True, session=session, **db)
    await engine.dispose()


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoIdentityColumn(AbstractTestInputsOfferNoIdentityColumn):
    pass


class TestAnUpdateKeepsItsRowAndModel(AbstractTestAnUpdateKeepsItsRowAndModel):
    pass


class TestACreateKeepsItsModel(AbstractTestACreateKeepsItsModel):
    pass


class TestALinkKeepsItsRowAndModel(AbstractTestALinkKeepsItsRowAndModel):
    pass
