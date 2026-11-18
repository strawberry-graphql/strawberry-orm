"""Every page and root list is cut by the database (SQLAlchemy).

Run under ``execute_sync`` with a ``Session``, under ``execute`` with the same
``Session``, and under ``execute`` with an ``AsyncSession``.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.bounded_reads import AbstractTestBoundedReads


class UbBase(DeclarativeBase):
    pass


class UbOwner(UbBase):
    __tablename__ = "ub_owner"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    items: Mapped[list["UbItem"]] = relationship(back_populates="owner")


class UbItem(UbBase):
    __tablename__ = "ub_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    bucket: Mapped[int]
    hidden: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("ub_owner.id"))
    owner: Mapped[UbOwner] = relationship(back_populates="items")


def rows():
    owners = [UbOwner(id=i, name=f"o{i}") for i in (1, 2, 3)]
    items = [
        UbItem(
            id=i,
            name=f"i{i:02}",
            bucket=i % 3,
            hidden=i in (2, 15),
            owner_id=(i - 1) // 10 + 1,
        )
        for i in range(1, 31)
    ]
    return [*owners, *items]


MODES = ["sync", "async execution", "async session"]


@pytest_asyncio.fixture(params=MODES)
async def database(request):
    if request.param == "async session":
        pytest.importorskip("greenlet")
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(UbBase.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all(rows())
            await session.commit()
            session.expunge_all()
            yield SimpleNamespace(
                mode=request.param, session=session, engine=engine.sync_engine
            )
        await engine.dispose()
        return
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    UbBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(rows())
        session.commit()
        session.expunge_all()
        yield SimpleNamespace(mode=request.param, session=session, engine=engine)


@pytest.fixture
def make_orm():
    def _make(**options):
        options.setdefault("lazy_resolution", "off")
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            warn_missing_scope=False,
            default_query_limit=5,
            **options,
        )

    return _make


@pytest.fixture
def Owner():
    return UbOwner


@pytest.fixture
def Item():
    return UbItem


@pytest.fixture
def hide():
    return lambda query: query.where(UbItem.hidden.is_(False))


@pytest.fixture
def all_items():
    return lambda: select(UbItem)


@pytest.fixture
def owner_items():
    return lambda owner: select(UbItem).where(UbItem.owner_id == owner.id)


@pytest.fixture
def seeded(database):
    return database


@pytest.fixture
def capture(database):
    @contextmanager
    def _capture():
        seen = SimpleNamespace(loaded=0, params=[])

        def before(conn, cursor, statement, params, context, executemany):
            seen.params.append(len(params or ()))

        def loaded(target, context):
            seen.loaded += 1

        database.session.expunge_all()
        event.listen(database.engine, "before_cursor_execute", before)
        event.listen(UbItem, "load", loaded)
        try:
            yield seen
        finally:
            event.remove(UbItem, "load", loaded)
            event.remove(database.engine, "before_cursor_execute", before)

    return _capture


@pytest.fixture
def run(database):
    async def _run(schema, query):
        context = {"session": database.session}
        if database.mode == "sync":
            return await asyncio.to_thread(
                schema.execute_sync, query, context_value=context
            )
        return await schema.execute(query, context_value=context)

    return _run


@pytest.mark.asyncio
class TestBoundedReads(AbstractTestBoundedReads):
    pass
