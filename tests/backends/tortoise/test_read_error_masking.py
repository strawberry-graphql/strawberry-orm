"""A read that fails tells the client one plain message (Tortoise)."""

import sqlite3
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiosqlite import Connection
from tortoise.exceptions import (
    BaseORMException,
    ConfigurationError,
    DBConnectionError,
    DoesNotExist,
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    MultipleObjectsReturned,
    NoValuesFetched,
    OperationalError,
    ParamsError,
    TransactionManagementError,
    UnSupportedError,
    ValidationError,
)

from strawberry_orm import StrawberryORM
from tests.abstract.read_error_masking import AbstractTestReadErrorMasking
from tests.backends.tortoise.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


FAMILIES = {
    "BaseORMException": BaseORMException,
    "ConfigurationError": ConfigurationError,
    "DBConnectionError": DBConnectionError,
    "DoesNotExist": DoesNotExist,
    "FieldError": FieldError,
    "IncompleteInstanceError": IncompleteInstanceError,
    "IntegrityError": IntegrityError,
    "MultipleObjectsReturned": MultipleObjectsReturned,
    "NoValuesFetched": NoValuesFetched,
    "OperationalError": OperationalError,
    "OverflowError": OverflowError,
    "ParamsError": ParamsError,
    "TransactionManagementError": TransactionManagementError,
    "UnSupportedError": UnSupportedError,
    "ValidationError": ValidationError,
    "sqlite3.DataError": sqlite3.DataError,
    "sqlite3.DatabaseError": sqlite3.DatabaseError,
    "sqlite3.IntegrityError": sqlite3.IntegrityError,
    "sqlite3.InterfaceError": sqlite3.InterfaceError,
    "sqlite3.NotSupportedError": sqlite3.NotSupportedError,
    "sqlite3.OperationalError": sqlite3.OperationalError,
    "sqlite3.ProgrammingError": sqlite3.ProgrammingError,
}
#: A database error and one of the ORM's own, raised on every read.
TYPICAL = ("OperationalError", "FieldError")


class TestReadErrorMasking(AbstractTestReadErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL

    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, execution, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    def failing(self, error):
        return refusing(error)


#: Where Tortoise's SQLite client hands a statement to the driver.
STATEMENTS = ("execute_insert", "execute_fetchall", "executemany")


@contextmanager
def refusing(error, verbs=("",)):
    """Every statement starting with one of *verbs* raises *error* as it runs."""

    def refuse_in(run):
        async def refuse(connection, sql, *args, **kwargs):
            if str(sql).lstrip().upper().startswith(verbs):
                raise error
            return await run(connection, sql, *args, **kwargs)

        return refuse

    with ExitStack() as stack:
        for name in STATEMENTS:
            run = getattr(Connection, name)
            stack.enter_context(patch.object(Connection, name, refuse_in(run)))
        yield
