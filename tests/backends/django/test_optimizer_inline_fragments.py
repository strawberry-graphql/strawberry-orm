"""Tests for optimizer handling of union inline fragments and fragment spreads."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from graphql import parse

from strawberry_orm.backends.django import DjangoBackend
from tests.abstract.optimizer_inline_fragments import (
    AbstractTestOptimizerInlineFragmentsSync,
    AbstractTestOptimizerInlineFragmentsUnitSync,
)


@pytest.fixture
def users_query():
    def _users_query(User, info):
        return User.objects.all()

    return _users_query


@pytest.fixture
def posts_query():
    def _posts_query(Post, info):
        return Post.objects.all()

    return _posts_query


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


@pytest.fixture
def schema_execute_with_queries():
    def _execute(schema, query):
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query)
        return result, [q["sql"] for q in ctx.captured_queries]

    return _execute


@pytest.fixture
def apply_optimizer_hints_inline_fragment(Post):
    def _apply(Post):
        backend = DjangoBackend()
        doc = parse(
            "{ posts { ... on PostBrief { title } ... on PostFull { title } } }"
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type("Info", (), {"field_nodes": [field_node], "fragments": {}})()
        return backend.apply_optimizer_hints(None, Post.objects.all(), info)

    return _apply


class TestOptimizerInlineFragments(AbstractTestOptimizerInlineFragmentsSync):
    @staticmethod
    def published_only(query, Post):
        return query.filter(is_published=True)


class TestOptimizerInlineFragmentsUnit(AbstractTestOptimizerInlineFragmentsUnitSync):
    pass
