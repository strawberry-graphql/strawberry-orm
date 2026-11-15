"""Filters that must not answer questions about hidden rows (SQLAlchemy)."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.filter_oracles import (
    AbstractTestCustomFiltersFollowTheirOperator,
    AbstractTestEdgeScopeOfTheReturnedType,
    AbstractTestFiltersThroughAReverseRelation,
    AbstractTestForeignKeyShortcutReadsOnlyTheKey,
    AbstractTestNegationAcrossAToManyRelation,
    AbstractTestReferenceLookupsKeepTheKeysType,
)
from tests.backends.sqlalchemy.models import Book, Comment, Post, Publisher, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(
        User=User, Post=Post, Comment=Comment, Tag=Tag, Publisher=Publisher, Book=Book
    )


@pytest.fixture
def narrow():
    return SimpleNamespace(
        equal=lambda query, model, field, value: query.where(
            getattr(model, field) == value
        ),
        exclude=lambda query, model, field, value: query.where(
            getattr(model, field) != value
        ),
        starts_with=lambda query, model, field, value: query.where(
            getattr(model, field).startswith(value)
        ),
    )


@pytest.fixture
def run(sa_session, seed):
    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": sa_session}
        )

    return _run


@pytest.fixture
def numeric_text_keys(sa_session, seed):
    sa_session.add_all(
        [
            Publisher(publisher_code="00123", name="Zero Padded"),
            Publisher(publisher_code="123", name="Plain"),
        ]
    )
    sa_session.flush()
    sa_session.add_all(
        [
            Book(id=1, title="Padded", publisher_code="00123"),
            Book(id=2, title="Plain Book", publisher_code="123"),
        ]
    )
    sa_session.commit()


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
