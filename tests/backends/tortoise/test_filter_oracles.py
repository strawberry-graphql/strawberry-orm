"""Filters that must not answer questions about hidden rows (Tortoise)."""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.filter_oracles import (
    AbstractTestCustomFiltersFollowTheirOperator,
    AbstractTestEdgeScopeOfTheReturnedType,
    AbstractTestFiltersThroughAReverseRelation,
    AbstractTestForeignKeyShortcutReadsOnlyTheKey,
    AbstractTestNegationAcrossAToManyRelation,
    AbstractTestReferenceLookupsKeepTheKeysType,
    AbstractTestTheScopeIsWrittenFirst,
)
from tests.backends.tortoise.models import Book, Comment, Post, Publisher, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(
        User=User, Post=Post, Comment=Comment, Tag=Tag, Publisher=Publisher, Book=Book
    )


@pytest.fixture
def narrow():
    return SimpleNamespace(
        equal=lambda query, model, field, value: query.filter(**{field: value}),
        exclude=lambda query, model, field, value: query.exclude(**{field: value}),
        starts_with=lambda query, model, field, value: query.filter(
            **{f"{field}__startswith": value}
        ),
    )


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, variables=None):
        return await schema.execute(query, variable_values=variables, context_value={})

    return _run


@pytest_asyncio.fixture
async def numeric_text_keys(seed):
    padded = await Publisher.create(publisher_code="00123", name="Zero Padded")
    plain = await Publisher.create(publisher_code="123", name="Plain")
    await Book.create(id=1, title="Padded", publisher=padded)
    await Book.create(id=2, title="Plain Book", publisher=plain)


class TestNegationAcrossAToManyRelation(AbstractTestNegationAcrossAToManyRelation):
    pass


class TestCustomFiltersFollowTheirOperator(
    AbstractTestCustomFiltersFollowTheirOperator
):
    pass


class TestEdgeScopeOfTheReturnedType(AbstractTestEdgeScopeOfTheReturnedType):
    pass


class TestForeignKeyShortcutReadsOnlyTheKey(
    AbstractTestForeignKeyShortcutReadsOnlyTheKey
):
    pass


class TestReferenceLookupsKeepTheKeysType(AbstractTestReferenceLookupsKeepTheKeysType):
    pass


class TestFiltersThroughAReverseRelation(AbstractTestFiltersThroughAReverseRelation):
    pass


class TestTheScopeIsWrittenFirst(AbstractTestTheScopeIsWrittenFirst):
    pass
