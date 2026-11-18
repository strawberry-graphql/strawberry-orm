"""A read that fails tells the client one plain message (SQLAlchemy)."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import (
    ArgumentError,
    CompileError,
    InvalidRequestError,
    MultipleResultsFound,
    NoResultFound,
    SQLAlchemyError,
    StatementError,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.exc import DetachedInstanceError, StaleDataError

from strawberry_orm import StrawberryORM
from tests.abstract.read_error_masking import AbstractTestReadErrorMasking
from tests.backends.sqlalchemy.models import Base, Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@pytest_asyncio.fixture
async def dz_async_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        author = User(id=1, name="Alice", email="alice@example.com")
        post = Post(id=1, title="Hello World", body="b", author=author)
        session.add_all(
            [author, post, Comment(id=1, body="Nice", post=post, author=author)]
        )
        await session.commit()
        yield session
    await engine.dispose()


FAMILIES = {
    "ArgumentError": ArgumentError,
    "CompileError": CompileError,
    "DetachedInstanceError": DetachedInstanceError,
    "InvalidRequestError": InvalidRequestError,
    "MultipleResultsFound": MultipleResultsFound,
    "NoResultFound": NoResultFound,
    "OverflowError": OverflowError,
    "SQLAlchemyError": SQLAlchemyError,
    "StaleDataError": StaleDataError,
    "StatementError": lambda message: StatementError(
        message, "SELECT * FROM dz_secret_ledger", {}, None
    ),
    "sqlite3.DataError": sqlite3.DataError,
    "sqlite3.DatabaseError": sqlite3.DatabaseError,
    "sqlite3.IntegrityError": sqlite3.IntegrityError,
    "sqlite3.InterfaceError": sqlite3.InterfaceError,
    "sqlite3.OperationalError": sqlite3.OperationalError,
    "sqlite3.ProgrammingError": sqlite3.ProgrammingError,
}
#: A database error and one of the ORM's own, raised on every read.
TYPICAL = ("sqlite3.OperationalError", "InvalidRequestError")


class TestReadErrorMasking(AbstractTestReadErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL
    EXECUTIONS = ("sync", "async")

    @pytest.fixture(autouse=True)
    def _sessions(self, sa_session, seed, dz_async_session):
        self.sessions = {"sync": sa_session, "async": dz_async_session}

    async def run(self, schema, query, execution, variables=None):
        context = {"session": self.sessions[execution]}
        if execution == "async":
            return await schema.execute(
                query, variable_values=variables, context_value=context
            )
        return schema.execute_sync(
            query, variable_values=variables, context_value=context
        )

    @contextmanager
    def failing(self, error):
        def refuse(*args, **kwargs):
            raise error

        engines = [
            self.sessions["sync"].get_bind(),
            self.sessions["async"].bind.sync_engine,
        ]
        for engine in engines:
            event.listen(engine, "before_cursor_execute", refuse)
        try:
            yield
        finally:
            for engine in engines:
                event.remove(engine, "before_cursor_execute", refuse)
