from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from strawberry_orm import StrawberryORM
from strawberry_orm.repo import _get_sa_pk_column
from tests.abstract.write_path_authorization import (
    AbstractTestAuthorizeKeepsTheRepo,
    AbstractTestLinkChecks,
    AbstractTestNodeInputsFollowTypeExclude,
    AbstractTestPermissionResults,
    AbstractTestPolicyWithRepos,
    AbstractTestProjectionDefaults,
    AbstractTestRefListDeleteUsesTheRepo,
    AbstractTestRefListIsAtomic,
    AbstractTestRefListMembership,
    AbstractTestUpsertSeed,
    WriteDb,
    resolve,
)
from tests.backends.sqlalchemy.models import Base, Comment, Post, Tag, User

MODELS = SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


@dataclass
class SQLAlchemyWriteDb(WriteDb):
    session: Any = None

    @property
    def context(self) -> dict[str, Any]:
        return {"session": self.session}

    def new_orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            warn_sensitive=False,
            **kwargs,
        )

    def only(self, query: Any, model: type, pks: list[int]) -> Any:
        return query.where(_get_sa_pk_column(model).in_(pks))

    async def refs(
        self, orm: Any, instance: Any, field: str, refs: list[Any], **kwargs: Any
    ) -> None:
        await super().refs(orm, instance, field, refs, **kwargs)
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
        statement = select(model).order_by(_get_sa_pk_column(model))
        result = await resolve(self.session.execute(statement))
        return list(result.scalars().all())

    async def reload(self, instance: Any) -> Any:
        await resolve(self.session.refresh(instance))
        return instance

    async def related(self, instance: Any, name: str) -> list[Any]:
        await resolve(self.session.refresh(instance, [name]))
        return sorted(getattr(instance, name), key=lambda row: row.id)


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


class TestRefListMembership(AbstractTestRefListMembership):
    pass


class TestAuthorizeKeepsTheRepo(AbstractTestAuthorizeKeepsTheRepo):
    pass


class TestPermissionResults(AbstractTestPermissionResults):
    pass


class TestUpsertSeed(AbstractTestUpsertSeed):
    pass


class TestNodeInputsFollowTypeExclude(AbstractTestNodeInputsFollowTypeExclude):
    pass


class TestProjectionDefaults(AbstractTestProjectionDefaults):
    pass


class TestLinkChecks(AbstractTestLinkChecks):
    pass


class TestRefListIsAtomic(AbstractTestRefListIsAtomic):
    pass


class TestPolicyWithRepos(AbstractTestPolicyWithRepos):
    pass


class TestRefListDeleteUsesTheRepo(AbstractTestRefListDeleteUsesTheRepo):
    pass
