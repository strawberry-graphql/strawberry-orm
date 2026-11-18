"""Shared optimizer tests for inline fragments and fragment spreads."""

from collections.abc import Callable
from typing import Annotated, Any

import pytest
import strawberry

from strawberry_orm.types import auto

UsersQueryFactory = Callable[[type, Any], Any]
PostsQueryFactory = Callable[[type, Any], Any]

UNION_LIST_QUERY = """
{
  users {
    id
    posts {
      __typename
      ... on PostBriefUnion { title }
      ... on PostFullUnion { title }
    }
  }
}
"""

#: Each user's posts, as the union types they went to, with every row shown.
UNION_POSTS = {
    "1": [("PostFullUnion", "GraphQL Guide"), ("PostFullUnion", "Hello World")],
    "2": [("PostBriefUnion", "Draft Post")],
    "3": [("PostFullUnion", "Rust Adventures")],
}


def union_list_schema(
    orm: Any,
    User: type,
    Post: type,
    users_query: UsersQueryFactory,
    published_only: Callable[[Any, type], Any] | None,
) -> strawberry.Schema:
    """Users whose posts list a union of two types over ``Post``, each deciding
    its own rows: a draft is brief, a published post full. Given
    *published_only*, the model's scope keeps only published posts."""

    @orm.type(Post, name="PostBriefUnion")
    class PostBriefUnion:
        id: auto
        title: auto

        @classmethod
        def is_type_of(cls, obj, info):
            return isinstance(obj, Post) and not obj.is_published

    @orm.type(Post, name="PostFullUnion")
    class PostFullUnion:
        id: auto
        title: auto

        @classmethod
        def is_type_of(cls, obj, info):
            return isinstance(obj, Post) and obj.is_published

        if published_only is not None:

            @classmethod
            def scope_rows(cls, query, info):
                return published_only(query, Post)

    PostUnion = Annotated[PostBriefUnion | PostFullUnion, strawberry.union("PostUnion")]

    @orm.type(User)
    class UsersUnionType:
        id: auto
        posts: list[PostUnion] = orm.field.auto()

    @strawberry.type
    class UnionListQuery:
        @strawberry.field
        def users(self, info: strawberry.types.Info) -> list[UsersUnionType]:
            return users_query(User, info)  # type: ignore[return-value]

    return strawberry.Schema(
        query=UnionListQuery, extensions=[orm.optimizer_extension()]
    )


def union_posts(result: Any, scoped: bool) -> None:
    assert result.errors is None, f"{result.errors}"
    found = {
        str(user["id"]): sorted(
            (post["__typename"], post["title"]) for post in user["posts"]
        )
        for user in result.data["users"]
    }
    expected = {**UNION_POSTS, "2": []} if scoped else UNION_POSTS
    assert found == expected, f"the union listed {found}, not {expected}"


class AbstractTestOptimizerInlineFragmentsSync:
    expected_user_count = 3
    max_optimizer_queries = 3
    require_equal_spread_query_count = True

    def test_apply_optimizer_hints_inline_fragments_do_not_crash(
        self, orm, seed, User, Post, schema_execute, users_query
    ):
        """Fields selected only via inline fragments must not raise."""

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto

        @orm.type(User)
        class UsersQueryType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class InlineFragmentsQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersQueryType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=InlineFragmentsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFull { title }
              ... on PostFull { id }
            }
          }
        }
        """
        result = schema_execute(schema, query)
        assert result.errors is None
        assert len(result.data["users"]) == self.expected_user_count

    def test_apply_optimizer_hints_union_branch_select_related(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        schema_execute_with_queries,
        users_query,
    ):
        """Relations inside an inline fragment should be eager-loaded."""

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersWithPostsType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class InlineFragmentBranchQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersWithPostsType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=InlineFragmentBranchQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFull {
                title
                tags { name }
              }
            }
          }
        }
        """
        result, queries = schema_execute_with_queries(schema, query)
        assert result.errors is None
        assert len(queries) <= self.max_optimizer_queries
        sql = " ".join(q.lower() for q in queries)
        assert "tag" in sql

    def test_apply_optimizer_hints_fragment_spread(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        schema_execute_with_queries,
        users_query,
    ):
        """Named fragment spreads should resolve and apply the same prefetches."""

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersFragmentSpreadType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class FragmentSpreadQuery:
            @strawberry.field
            def users(
                self, info: strawberry.types.Info
            ) -> list[UsersFragmentSpreadType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=FragmentSpreadQuery, extensions=[orm.optimizer_extension()]
        )
        plain_query = "{ users { posts { tags { name } } } }"
        spread_query = """
        fragment PostTagFields on PostFull {
          tags { name }
        }
        {
          users {
            posts {
              ...PostTagFields
            }
          }
        }
        """
        plain_result, plain_queries = schema_execute_with_queries(schema, plain_query)
        spread_result, spread_queries = schema_execute_with_queries(
            schema, spread_query
        )

        assert plain_result.errors is None
        assert spread_result.errors is None
        assert len(plain_queries) <= self.max_optimizer_queries
        if self.require_equal_spread_query_count:
            assert len(spread_queries) == len(plain_queries)
        assert len(spread_queries) <= self.max_optimizer_queries

    def test_apply_optimizer_hints_nested_union_fragments(
        self, orm, seed, User, Post, Tag, schema_execute, users_query
    ):
        """Nested relation fields under an inline fragment must not crash."""

        @orm.type(Tag, name="TagType")
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFullNested")
        class PostFullNested:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersNestedType:
            id: auto
            posts: list[PostFullNested] = orm.field.auto()

        @strawberry.type
        class NestedInlineFragmentQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersNestedType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=NestedInlineFragmentQuery, extensions=[orm.optimizer_extension()]
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
        result = schema_execute(schema, query)
        assert result.errors is None

    def test_apply_optimizer_hints_plain_fields_unchanged(
        self,
        orm,
        seed,
        Post,
        User,
        schema_execute_with_queries,
        users_query,
    ):
        """Plain field selections (no fragments) must keep working."""

        @orm.type(Post)
        class PlainPostType:
            id: auto
            title: auto

        @orm.type(User)
        class PlainUsersType:
            id: auto
            posts: list[PlainPostType] = orm.field.auto()

        @strawberry.type
        class PlainFieldsQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[PlainUsersType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PlainFieldsQuery, extensions=[orm.optimizer_extension()]
        )
        query = "{ users { posts { title } } }"

        result, queries = schema_execute_with_queries(schema, query)
        assert result.errors is None
        assert len(queries) <= self.max_optimizer_queries

    def test_apply_optimizer_hints_polymorphic_list_no_crash(
        self, orm, seed, Post, schema_execute, posts_query
    ):
        """Root list with inline-fragment-only selection must not crash."""

        @orm.type(Post, name="PostFullRoot")
        class PostFullRoot:
            id: auto
            title: auto

        @strawberry.type
        class PolymorphicPostsQuery:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostFullRoot]:
                return posts_query(Post, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PolymorphicPostsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          posts {
            ... on PostFullRoot { title }
            ... on PostFullRoot { id }
          }
        }
        """
        result = schema_execute(schema, query)
        assert result.errors is None

    @pytest.mark.parametrize("scoped", [False, True], ids=["shown", "drafts hidden"])
    def test_graphql_union_list_inline_fragments(
        self, orm, seed, User, Post, schema_execute, users_query, scoped
    ):
        """Each row of a relation listing a union goes to the type its
        ``is_type_of`` picks, under the scope of its model's rows."""
        published_only = self.published_only if scoped else None
        schema = union_list_schema(orm, User, Post, users_query, published_only)
        union_posts(schema_execute(schema, UNION_LIST_QUERY), scoped)


class AbstractTestOptimizerInlineFragmentsAsync:
    """Async variant for Tortoise — mirrors :class:`AbstractTestOptimizerInlineFragmentsSync`."""

    expected_user_count = AbstractTestOptimizerInlineFragmentsSync.expected_user_count
    max_optimizer_queries = 20
    require_equal_spread_query_count = False

    async def test_apply_optimizer_hints_inline_fragments_do_not_crash(
        self, orm, seed, User, Post, schema_execute_async, users_query
    ):
        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto

        @orm.type(User)
        class UsersQueryType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class InlineFragmentsQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersQueryType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=InlineFragmentsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFull { title }
              ... on PostFull { id }
            }
          }
        }
        """
        result = await schema_execute_async(schema, query)
        assert result.errors is None
        assert len(result.data["users"]) == self.expected_user_count

    async def test_apply_optimizer_hints_union_branch_select_related(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        schema_execute_with_queries_async,
        users_query,
    ):
        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersWithPostsType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class InlineFragmentBranchQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersWithPostsType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=InlineFragmentBranchQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          users {
            posts {
              ... on PostFull {
                title
                tags { name }
              }
            }
          }
        }
        """
        result, queries = await schema_execute_with_queries_async(schema, query)
        assert result.errors is None
        assert len(queries) <= self.max_optimizer_queries
        sql = " ".join(q.lower() for q in queries)
        assert "tag" in sql

    async def test_apply_optimizer_hints_fragment_spread(
        self,
        orm,
        seed,
        User,
        Post,
        Tag,
        schema_execute_with_queries_async,
        users_query,
    ):
        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFull")
        class PostFull:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersFragmentSpreadType:
            id: auto
            posts: list[PostFull] = orm.field.auto()

        @strawberry.type
        class FragmentSpreadQuery:
            @strawberry.field
            def users(
                self, info: strawberry.types.Info
            ) -> list[UsersFragmentSpreadType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=FragmentSpreadQuery, extensions=[orm.optimizer_extension()]
        )
        plain_query = "{ users { posts { tags { name } } } }"
        spread_query = """
        fragment PostTagFields on PostFull {
          tags { name }
        }
        {
          users {
            posts {
              ...PostTagFields
            }
          }
        }
        """
        plain_result, plain_queries = await schema_execute_with_queries_async(
            schema, plain_query
        )
        spread_result, spread_queries = await schema_execute_with_queries_async(
            schema, spread_query
        )

        assert plain_result.errors is None
        assert spread_result.errors is None
        assert len(plain_queries) <= self.max_optimizer_queries
        if self.require_equal_spread_query_count:
            assert len(spread_queries) == len(plain_queries)
        assert len(spread_queries) <= self.max_optimizer_queries

    async def test_apply_optimizer_hints_nested_union_fragments(
        self, orm, seed, User, Post, Tag, schema_execute_async, users_query
    ):
        @orm.type(Tag, name="TagType")
        class TagType:
            id: auto
            name: auto

        @orm.type(Post, name="PostFullNested")
        class PostFullNested:
            id: auto
            title: auto
            tags: list[TagType]

        @orm.type(User)
        class UsersNestedType:
            id: auto
            posts: list[PostFullNested] = orm.field.auto()

        @strawberry.type
        class NestedInlineFragmentQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UsersNestedType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=NestedInlineFragmentQuery, extensions=[orm.optimizer_extension()]
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
        result = await schema_execute_async(schema, query)
        assert result.errors is None

    async def test_apply_optimizer_hints_plain_fields_unchanged(
        self,
        orm,
        seed,
        Post,
        User,
        schema_execute_with_queries_async,
        users_query,
    ):
        @orm.type(Post)
        class PlainPostType:
            id: auto
            title: auto

        @orm.type(User)
        class PlainUsersType:
            id: auto
            posts: list[PlainPostType] = orm.field.auto()

        @strawberry.type
        class PlainFieldsQuery:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[PlainUsersType]:
                return users_query(User, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PlainFieldsQuery, extensions=[orm.optimizer_extension()]
        )
        query = "{ users { posts { title } } }"

        result, queries = await schema_execute_with_queries_async(schema, query)
        assert result.errors is None
        assert len(queries) <= self.max_optimizer_queries

    async def test_apply_optimizer_hints_polymorphic_list_no_crash(
        self, orm, seed, Post, schema_execute_async, posts_query
    ):
        @orm.type(Post, name="PostFullRoot")
        class PostFullRoot:
            id: auto
            title: auto

        @strawberry.type
        class PolymorphicPostsQuery:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostFullRoot]:
                return posts_query(Post, info)  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=PolymorphicPostsQuery, extensions=[orm.optimizer_extension()]
        )
        query = """
        {
          posts {
            ... on PostFullRoot { title }
            ... on PostFullRoot { id }
          }
        }
        """
        result = await schema_execute_async(schema, query)
        assert result.errors is None

    @pytest.mark.parametrize("scoped", [False, True], ids=["shown", "drafts hidden"])
    async def test_graphql_union_list_inline_fragments(
        self, orm, seed, User, Post, schema_execute_async, users_query, scoped
    ):
        """Each row of a relation listing a union goes to the type its
        ``is_type_of`` picks, under the scope of its model's rows."""
        published_only = self.published_only if scoped else None
        schema = union_list_schema(orm, User, Post, users_query, published_only)
        union_posts(await schema_execute_async(schema, UNION_LIST_QUERY), scoped)


class AbstractTestOptimizerInlineFragmentsUnitSync:
    def test_walk_selections_handles_inline_fragment_ast(
        self, Post, apply_optimizer_hints_inline_fragment
    ):
        """Direct call walks inline fragment field selections without error."""
        result = apply_optimizer_hints_inline_fragment(Post)
        assert isinstance(result, list)


class AbstractTestOptimizerInlineFragmentsUnitAsync:
    async def test_walk_selections_handles_inline_fragment_ast(
        self, Post, apply_optimizer_hints_inline_fragment
    ):
        """Direct call walks inline fragment field selections without error."""
        result = await apply_optimizer_hints_inline_fragment(Post)
        assert isinstance(result, list)
