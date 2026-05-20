"""Schema and fixtures for Publisher (non-id PK) / Book relation presence tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
import strawberry
from tortoise import Tortoise

from strawberry_orm import StrawberryORM
from tests.backends.tortoise.models import Book as TortBook
from tests.backends.tortoise.models import Publisher as TortPublisher

_custom_pk_orm = StrawberryORM.for_tortoise()

PublisherFilter = _custom_pk_orm.filter(TortPublisher)
BookFilter = _custom_pk_orm.filter(TortBook)


@_custom_pk_orm.type(TortPublisher, filters=PublisherFilter)
class PublisherType:
    publisher_code: str
    name: str


@_custom_pk_orm.type(TortBook, filters=BookFilter)
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

_TORTOISE_MODELS = [
    "tests.backends.tortoise.models",
]


@pytest_asyncio.fixture
async def custom_pk_db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": _TORTOISE_MODELS},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


@pytest.fixture
def custom_pk_orm():
    return _custom_pk_orm


@pytest.fixture
def Publisher():
    return TortPublisher


@pytest.fixture
def Book():
    return TortBook


@pytest_asyncio.fixture
async def custom_pk_seed(custom_pk_db):
    await TortBook.all().delete()
    await TortPublisher.all().delete()
    penguin = await TortPublisher.create(publisher_code="PEN", name="Penguin")
    ace = await TortPublisher.create(publisher_code="ACE", name="Ace Books")
    await TortBook.create(id=1, title="Dune", publisher=penguin)
    await TortBook.create(id=2, title="Neuromancer", publisher=ace)
    return {"publishers": {"penguin": penguin, "ace": ace}}


@pytest_asyncio.fixture
async def custom_pk_execute(custom_pk_seed):
    async def _execute(query, variables=None):
        result = await custom_pk_schema.execute(
            query,
            variable_values=variables or {},
        )
        assert result.errors is None, f"GraphQL errors: {result.errors}"
        return result.data

    return _execute
