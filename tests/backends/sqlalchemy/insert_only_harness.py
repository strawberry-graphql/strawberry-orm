"""The SQLAlchemy ``db`` fixture for the upsert tests, on a sync and an async session."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from strawberry_orm import StrawberryORM
from tests.abstract.insert_only_upserts import UpsertDb, build_schema, resolve
from tests.backends.sqlalchemy.insert_only_models import (
    Address,
    Event,
    Group,
    InsertOnlyBase,
    Person,
)
from tests.backends.sqlalchemy.models import Base, Book, Post, Publisher, Tag, User

ORM = StrawberryORM.for_sqlalchemy(
    dialect="sqlite",
    lazy_resolution="off",
    warn_missing_scope=False,
    warn_sensitive=False,
)
BUILT = build_schema(
    ORM,
    SimpleNamespace(
        Person=Person,
        Address=Address,
        Event=Event,
        Group=Group,
        User=User,
        Post=Post,
        Tag=Tag,
        Publisher=Publisher,
        Book=Book,
    ),
)


@dataclass
class SQLAlchemyDb(UpsertDb):
    session: Any = None

    @property
    def context(self) -> dict[str, Any]:
        return {"session": self.session}

    async def apply_refs(self, instance: Any, name: str, refs: list[Any]) -> None:
        await super().apply_refs(instance, name, refs)
        await resolve(self.session.flush())

    async def create(self, model: type, **values: Any) -> Any:
        instance = model(**values)
        self.session.add(instance)
        await resolve(self.session.commit())
        return instance

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        await resolve(self.session.refresh(instance, [name]))
        getattr(instance, name).extend(rows)
        await resolve(self.session.commit())

    async def rows(self, model: type) -> list[Any]:
        pk = model.__mapper__.primary_key[0]
        result = await resolve(self.session.execute(select(model).order_by(pk)))
        return list(result.scalars().all())

    async def reload(self, instance: Any) -> Any:
        await resolve(self.session.refresh(instance))
        return instance

    async def related(self, instance: Any, name: str) -> list[Any]:
        await resolve(self.session.refresh(instance, [name]))
        return list(getattr(instance, name))


def _record(engine: Any, statements: list[str]) -> None:
    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)


@pytest_asyncio.fixture(params=["sync", "async"])
async def db(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        InsertOnlyBase.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        harness = SQLAlchemyDb(BUILT, async_hooks=False, session=session)
        _record(engine, harness.statements)
        yield harness
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(InsertOnlyBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        harness = SQLAlchemyDb(BUILT, async_hooks=True, session=session)
        _record(engine.sync_engine, harness.statements)
        yield harness
    await engine.dispose()
