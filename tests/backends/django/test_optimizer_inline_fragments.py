"""Tests for optimizer handling of union inline fragments and fragment spreads."""

from typing import Annotated

import pytest
import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext
from graphql import parse

from strawberry_orm.backends.django import DjangoBackend
from strawberry_orm.types import auto


@pytest.mark.django_db
class TestOptimizerInlineFragments:
    def test_apply_optimizer_hints_inline_fragments_do_not_crash(self, orm, seed, User):
        """Union list field selected only via inline fragments must not raise."""
        from tests.backends.django.models import Post

        @orm.type(Post, name="PostBrief")
        class PostBrief:
            id: auto
            title: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto

        PostUnion = Annotated[PostBrief | PostFull, strawberry.union("PostUnion")]

        @orm.type(User)
        class UsersQueryType:
            id: auto
            posts: list[PostUnion] = orm.field()

        @strawberry.type
        class UnionFragmentsQuery:
            @strawberry.field
            def users(self) -> list[UsersQueryType]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=UnionFragmentsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostBrief { title }
              ... on PostFull { title }
            }
          }
        }
        """
        result = schema.execute_sync(query)
        assert result.errors is None
        assert len(result.data["users"]) == 3

    def test_apply_optimizer_hints_union_branch_select_related(self, orm, seed, User):
        """Relations inside an inline fragment should be eager-loaded."""
        from tests.backends.django.models import Post

        @orm.type(Post, name="PostBrief")
        class PostBrief:
            id: auto
            title: auto

        @orm.type(User, name="AuthorType")
        class AuthorType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            author: AuthorType

        PostUnion = Annotated[PostBrief | PostFull, strawberry.union("PostUnion")]

        @orm.type(User)
        class UsersWithPostsUnionType:
            id: auto
            posts: list[PostUnion] = orm.field()

        @strawberry.type
        class UnionBranchQuery:
            @strawberry.field
            def users(self) -> list[UsersWithPostsUnionType]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=UnionBranchQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFull {
                title
                author { name }
              }
            }
          }
        }
        """
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query)

        assert result.errors is None
        assert len(ctx) <= 2
        sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
        assert "author" in sql

    def test_apply_optimizer_hints_fragment_spread(self, orm, seed, User):
        """Named fragment spreads should resolve and apply the same prefetches."""
        from tests.backends.django.models import Post

        @orm.type(User, name="AuthorType")
        class AuthorType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            author: AuthorType

        @orm.type(User)
        class UsersFragmentSpreadType:
            id: auto
            posts: list[PostFull] = orm.field()

        @strawberry.type
        class FragmentSpreadQuery:
            @strawberry.field
            def users(self) -> list[UsersFragmentSpreadType]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=FragmentSpreadQuery, extensions=[orm.optimizer_extension()]
        )
        plain_query = "{ users { posts { author { name } } } }"
        spread_query = """
        fragment PostAuthorFields on PostFull {
          author { name }
        }
        {
          users {
            posts {
              ...PostAuthorFields
            }
          }
        }
        """
        with CaptureQueriesContext(connection) as plain_ctx:
            plain_result = schema.execute_sync(plain_query)
        with CaptureQueriesContext(connection) as spread_ctx:
            spread_result = schema.execute_sync(spread_query)

        assert plain_result.errors is None
        assert spread_result.errors is None
        # Plain fields and fragment spreads should apply the same eager loads.
        assert len(plain_ctx) <= 2
        assert len(spread_ctx) == len(plain_ctx)
        assert len(spread_ctx) <= 2

    def test_apply_optimizer_hints_nested_union_fragments(self, orm, seed, User, Tag):
        """Nested relation fields under a union inline fragment must not crash."""
        from tests.backends.django.models import Post

        @orm.type(Post, name="PostBriefNested")
        class PostBriefNested:
            id: auto
            title: auto

        @orm.type(Tag, name="TagType")
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFullNested")
        class PostFullNested:
            id: auto
            title: auto
            tags: list[TagType]

        PostUnion = Annotated[
            PostBriefNested | PostFullNested, strawberry.union("PostUnionNested")
        ]

        @orm.type(User)
        class UsersNestedUnionType:
            id: auto
            posts: list[PostUnion] = orm.field()

        @strawberry.type
        class NestedUnionQuery:
            @strawberry.field
            def users(self) -> list[UsersNestedUnionType]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=NestedUnionQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFullNested {
                title
                tags { name }
              }
            }
          }
        }
        """
        result = schema.execute_sync(query)
        assert result.errors is None

    def test_apply_optimizer_hints_plain_fields_unchanged(self, orm, seed, Post, User):
        """Plain field selections (no fragments) must keep working."""

        @orm.type(Post)
        class PlainPostType:
            id: auto
            title: auto

        @orm.type(User)
        class PlainUsersType:
            id: auto
            posts: list[PlainPostType] = orm.field()

        @strawberry.type
        class PlainFieldsQuery:
            @strawberry.field
            def users(self) -> list[PlainUsersType]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PlainFieldsQuery, extensions=[orm.optimizer_extension()]
        )
        query = "{ users { posts { title } } }"

        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query)

        assert result.errors is None
        assert len(ctx) <= 2

    def test_apply_optimizer_hints_polymorphic_list_no_crash(self, orm, seed, Post):
        """Root list typed as union with only inline fragments must not crash."""

        @orm.type(Post, name="PostBriefRoot")
        class PostBriefRoot:
            id: auto
            title: auto

        @orm.type(Post, name="PostFullRoot")
        class PostFullRoot:
            id: auto
            title: auto

        PostUnionRoot = Annotated[
            PostBriefRoot | PostFullRoot, strawberry.union("PostUnionRoot")
        ]

        @strawberry.type
        class PolymorphicPostsQuery:
            @strawberry.field
            def posts(self) -> list[PostUnionRoot]:
                return Post.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PolymorphicPostsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          posts {
            ... on PostBriefRoot { title }
            ... on PostFullRoot { title }
          }
        }
        """
        result = schema.execute_sync(query)
        assert result.errors is None


class TestOptimizerInlineFragmentsUnit:
    @pytest.mark.django_db
    def test_walk_selections_handles_inline_fragment_ast(self, Post):
        """Direct call walks inline fragment field selections without error."""
        backend = DjangoBackend()
        doc = parse(
            "{ posts { ... on PostBrief { title } ... on PostFull { title } } }"
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        info = type("Info", (), {"field_nodes": [field_node], "fragments": {}})()

        result = backend.apply_optimizer_hints(None, Post.objects.all(), info)
        assert isinstance(result, list)
