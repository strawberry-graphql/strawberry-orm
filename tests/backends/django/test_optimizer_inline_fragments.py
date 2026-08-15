"""Tests for optimizer handling of union inline fragments and fragment spreads."""

from typing import Annotated

import pytest
import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext
from graphql import parse

from strawberry_orm.backends.django import DjangoBackend
from strawberry_orm.types import auto
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
    @pytest.mark.django_db
    def test_graphql_union_list_inline_fragments(
        self, orm, seed, User, Post, schema_execute, users_query
    ):
        """Union list fields selected only via inline fragments (Django runtime)."""

        @orm.type(Post, name="PostBriefUnion")
        class PostBriefUnion:
            id: auto
            title: auto

        @orm.type(Post, name="PostFullUnion")
        class PostFullUnion:
            id: auto
            title: auto

        PostUnion = Annotated[
            PostBriefUnion | PostFullUnion, strawberry.union("PostUnion")
        ]

        @orm.type(User)
        class UsersUnionType:
            id: auto
            posts: list[PostUnion] = orm.field.auto()

        @strawberry.type
        class UnionListQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersUnionType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=UnionListQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostBriefUnion { title }
              ... on PostFullUnion { title }
            }
          }
        }
        """
        result = schema_execute(schema, query)
        assert result.errors is None
        assert len(result.data["users"]) == self.expected_user_count


class TestOptimizerInlineFragmentsUnit(AbstractTestOptimizerInlineFragmentsUnitSync):
    pass
