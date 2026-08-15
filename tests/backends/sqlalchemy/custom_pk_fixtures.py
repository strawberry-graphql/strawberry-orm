"""Schema and fixtures for Publisher (non-id PK) / Book relation presence tests."""

from __future__ import annotations

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Book as SABook
from tests.backends.sqlalchemy.models import Publisher as SAPublisher

_custom_pk_orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

PublisherFilter = _custom_pk_orm.filter(SAPublisher)
BookFilter = _custom_pk_orm.filter(SABook)


@_custom_pk_orm.type(SAPublisher, filters=PublisherFilter)
class PublisherType:
    publisher_code: str
    name: str


@_custom_pk_orm.type(SABook, filters=BookFilter)
class BookType:
    id: int
    title: str
    publisher: PublisherType


@strawberry.type
class CustomPkQuery:
    books: list[BookType] = _custom_pk_orm.field.auto(filters=BookFilter)


def _build_custom_pk_schema(engine):
    return strawberry.Schema(
        query=CustomPkQuery,
        extensions=[_custom_pk_orm.optimizer_extension()],
    )


@pytest.fixture
def custom_pk_orm():
    return _custom_pk_orm


@pytest.fixture
def Publisher():
    return SAPublisher


@pytest.fixture
def Book():
    return SABook


@pytest.fixture
def custom_pk_seed(sa_session):
    engine = sa_session.get_bind()
    SABase.metadata.create_all(engine, tables=[SAPublisher.__table__, SABook.__table__])
    sa_session.query(SABook).delete()
    sa_session.query(SAPublisher).delete()
    sa_session.commit()
    penguin = SAPublisher(publisher_code="PEN", name="Penguin")
    ace = SAPublisher(publisher_code="ACE", name="Ace Books")
    sa_session.add_all([penguin, ace])
    sa_session.flush()
    sa_session.add_all(
        [
            SABook(id=1, title="Dune", publisher_code="PEN"),
            SABook(id=2, title="Neuromancer", publisher_code="ACE"),
        ]
    )
    sa_session.commit()
    return {"publishers": {"penguin": penguin, "ace": ace}}


@pytest.fixture
def custom_pk_execute(custom_pk_seed, sa_session):
    schema = _build_custom_pk_schema(sa_session.get_bind())

    def _execute(query, variables=None):
        result = schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value={"session": sa_session},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute
