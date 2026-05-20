"""Schema and fixtures for Publisher (non-id PK) / Book relation presence tests."""

from __future__ import annotations

import pytest
import strawberry
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.backends.django.models import Book as DjBook
from tests.backends.django.models import Publisher as DjPublisher

_custom_pk_orm = StrawberryORM.for_django(lazy_resolution="off")

PublisherFilter = _custom_pk_orm.filter(DjPublisher)
BookFilter = _custom_pk_orm.filter(DjBook)


@_custom_pk_orm.type(DjPublisher, filters=PublisherFilter)
class PublisherType:
    publisher_code: str
    name: str


@_custom_pk_orm.type(DjBook, filters=BookFilter)
class BookType:
    id: int
    title: str
    publisher: PublisherType


@strawberry.type
class CustomPkQuery:
    books: list[BookType] = _custom_pk_orm.field(filters=BookFilter)


custom_pk_schema = strawberry.Schema(
    query=CustomPkQuery,
    extensions=[_custom_pk_orm.optimizer_extension()],
)


def _ensure_publisher_book_tables() -> None:
    existing = set(connection.introspection.table_names())
    for model in (DjPublisher, DjBook):
        if model._meta.db_table not in existing:
            with connection.schema_editor() as editor:
                editor.create_model(model)


def _flush_publisher_book_tables() -> None:
    DjBook.objects.all().delete()
    DjPublisher.objects.all().delete()


@pytest.fixture
def custom_pk_orm():
    return _custom_pk_orm


@pytest.fixture
def Publisher():
    return DjPublisher


@pytest.fixture
def custom_pk_seed(setup_tables):
    _ensure_publisher_book_tables()
    _flush_publisher_book_tables()
    penguin = DjPublisher.objects.create(publisher_code="PEN", name="Penguin")
    ace = DjPublisher.objects.create(publisher_code="ACE", name="Ace Books")
    DjBook.objects.create(id=1, title="Dune", publisher=penguin)
    DjBook.objects.create(id=2, title="Neuromancer", publisher=ace)
    return {"publishers": {"penguin": penguin, "ace": ace}}


@pytest.fixture
def custom_pk_execute(custom_pk_seed):
    def _execute(query, variables=None):
        result = custom_pk_schema.execute_sync(
            query,
            variable_values=variables or {},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute
