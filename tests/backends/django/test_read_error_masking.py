"""A read that fails tells the client one plain message (Django)."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import (
    EmptyResultSet,
    FieldDoesNotExist,
    FieldError,
    FieldFetchBlocked,
    FullResultSet,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    ObjectNotUpdated,
    ValidationError,
)
from django.db import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from django.db.backends.utils import CursorWrapper

from strawberry_orm import StrawberryORM
from tests.abstract.read_error_masking import AbstractTestReadErrorMasking
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


FAMILIES = {
    "DataError": DataError,
    "DatabaseError": DatabaseError,
    "EmptyResultSet": EmptyResultSet,
    "Error": Error,
    "FieldDoesNotExist": FieldDoesNotExist,
    "FieldError": FieldError,
    "FieldFetchBlocked": FieldFetchBlocked,
    "FullResultSet": FullResultSet,
    "IntegrityError": IntegrityError,
    "InterfaceError": InterfaceError,
    "InternalError": InternalError,
    "MultipleObjectsReturned": MultipleObjectsReturned,
    "NotSupportedError": NotSupportedError,
    "ObjectDoesNotExist": ObjectDoesNotExist,
    "ObjectNotUpdated": ObjectNotUpdated,
    "OperationalError": OperationalError,
    "OverflowError": OverflowError,
    "ProgrammingError": ProgrammingError,
    "ValidationError": ValidationError,
    "sqlite3.DatabaseError": sqlite3.DatabaseError,
    "sqlite3.InterfaceError": sqlite3.InterfaceError,
    "sqlite3.ProgrammingError": sqlite3.ProgrammingError,
}
#: A database error and one of the ORM's own, raised on every read.
TYPICAL = ("OperationalError", "FieldError")


class TestReadErrorMasking(AbstractTestReadErrorMasking):
    FAMILIES = FAMILIES
    TYPICAL = TYPICAL
    EXECUTIONS = ("sync", "async")

    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, execution, variables=None):
        if execution == "async":
            return await schema.execute(
                query, variable_values=variables, context_value={}
            )
        return await sync_to_async(
            lambda: schema.execute_sync(
                query, variable_values=variables, context_value={}
            ),
            thread_sensitive=True,
        )()

    @contextmanager
    def failing(self, error):
        def refuse(*args, **kwargs):
            raise error

        with (
            patch.object(CursorWrapper, "execute", refuse),
            patch.object(CursorWrapper, "executemany", refuse),
        ):
            yield
