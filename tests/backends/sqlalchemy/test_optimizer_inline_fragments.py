"""Tests for optimizer handling of union inline fragments and fragment spreads."""

import pytest
from graphql import parse
from sqlalchemy import select

from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from tests.abstract.optimizer_inline_fragments import (
    AbstractTestOptimizerInlineFragmentsSync,
    AbstractTestOptimizerInlineFragmentsUnitSync,
)


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return select(User)

    return _users_query


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        return select(Post)

    return _posts_query


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


@pytest.fixture
def schema_execute_with_queries(sa_session, query_counter):
    def _execute(schema, query):
        start = len(query_counter)
        result = schema.execute_sync(query, context_value={"session": sa_session})
        return result, query_counter[start:]

    return _execute


@pytest.fixture
def apply_optimizer_hints_inline_fragment(sa_session, Post):
    def _apply(Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        doc = parse(
            "{ posts { ... on PostBrief { title } ... on PostFull { title } } }"
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type(
            "Info",
            (),
            {
                "field_nodes": [field_node],
                "fragments": {},
                "context": {"session": sa_session},
            },
        )()
        return backend.apply_optimizer_hints(None, select(Post), info)

    return _apply


class TestOptimizerInlineFragments(AbstractTestOptimizerInlineFragmentsSync):
    @pytest.mark.skip(
        reason="GraphQL union list runtime mapping is only exercised on Django"
    )
    def test_graphql_union_list_inline_fragments(self) -> None:
        pass


class TestOptimizerInlineFragmentsUnit(AbstractTestOptimizerInlineFragmentsUnitSync):
    pass
