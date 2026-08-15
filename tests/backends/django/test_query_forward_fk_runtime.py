"""Forward-FK query shapes that exercise nested author traversal paths."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryForwardFKRuntime:
    def _build_schema(self, User, Post, *, author_scope_rows=None):
        orm = StrawberryORM.for_django()

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto
            email: auto

            if author_scope_rows is not None:

                @classmethod
                def scope_rows(cls, qs, info):
                    return author_scope_rows(qs, info)

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()
            posts: list[PostType] = orm.field.auto()

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_root_posts_can_select_forward_fk(self, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "posts": [
                {"title": "Hello World", "author": {"name": "Alice"}},
                {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                {"title": "Draft Post", "author": {"name": "Bob"}},
                {"title": "Rust Adventures", "author": {"name": "Charlie"}},
            ]
        }

    def test_prefetched_reverse_relations_can_select_nested_forward_fk(
        self, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                users {
                    name
                    posts {
                        title
                        author { name }
                    }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World", "author": {"name": "Alice"}},
                        {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                    ],
                },
                {
                    "name": "Bob",
                    "posts": [{"title": "Draft Post", "author": {"name": "Bob"}}],
                },
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures", "author": {"name": "Charlie"}}
                    ],
                },
            ]
        }

    def test_forward_fk_respects_type_level_scope_rows(self, seed, User, Post):
        schema = self._build_schema(
            User,
            Post,
            author_scope_rows=lambda qs, info: qs.filter(email__contains="@"),
        )
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name email }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "posts": [
                {
                    "title": "Hello World",
                    "author": {"name": "Alice", "email": "alice@example.com"},
                },
                {
                    "title": "GraphQL Guide",
                    "author": {"name": "Alice", "email": "alice@example.com"},
                },
                {
                    "title": "Draft Post",
                    "author": {"name": "Bob", "email": "bob@example.com"},
                },
                {
                    "title": "Rust Adventures",
                    "author": {"name": "Charlie", "email": "charlie@test.org"},
                },
            ]
        }


@pytest.mark.django_db
class TestForwardFKScopingOnMaterializedParents:
    """A to-one relation must be scoped however the parent rows arrived.

    Returning rows instead of a query must not turn off ``scope_rows`` on the
    other end of the relation.
    """

    def _build_schema(self, User, Post, *, materialize):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(name="Alice")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                queryset = Post.objects.order_by("id")
                return list(queryset) if materialize else queryset

        return orm.schema(query=Query)

    def _run(self, User, Post, *, materialize):
        schema = self._build_schema(User, Post, materialize=materialize)
        result = schema.execute_sync(
            "{ posts { title author { name } } }", context_value={}
        )
        assert result.errors is None, result.errors
        return result.data["posts"]

    def test_materialized_parents_do_not_bypass_to_one_scoping(self, seed, User, Post):
        materialized = self._run(User, Post, materialize=True)
        lazy = self._run(User, Post, materialize=False)
        assert materialized == lazy
        assert [row["author"] for row in materialized] == [
            {"name": "Alice"},
            {"name": "Alice"},
            None,
            None,
        ]

    def test_an_unscoped_to_one_reads_straight_through(self, seed, User, Post):
        """With no scope there is nothing to reapply, so the row is returned."""
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                return list(Post.objects.order_by("id"))

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title author { name } } }", context_value={}
        )
        assert result.errors is None, result.errors
        assert [row["author"]["name"] for row in result.data["posts"]] == [
            "Alice",
            "Alice",
            "Bob",
            "Charlie",
        ]

    def test_a_null_to_one_stays_null_under_a_scope(self, seed, Comment):
        """Nothing on the other end means nothing to scope, and no query."""
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(Comment, name="ParentComment")
        class ParentType:
            id: auto
            body: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.exclude(body="unreachable")

        @orm.type(Comment, name="ReplyComment")
        class ReplyType:
            id: auto
            body: auto
            parent: ParentType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def comments(self, info: strawberry.types.Info) -> list[ReplyType]:
                return list(Comment.objects.order_by("id"))

        result = orm.schema(query=Query).execute_sync(
            "{ comments { body parent { body } } }", context_value={}
        )
        assert result.errors is None, result.errors
        parents = [row["parent"] for row in result.data["comments"]]
        assert parents[0] is None
        assert parents[1] == {"body": "Nice post!"}

    def test_an_already_loaded_to_one_is_taken_as_is(self, seed, User, Post):
        """A loaded relation is trusted, so no second query is issued.

        Whatever loaded it is responsible for scoping it - which is what the
        optimizer does when it builds the eager load.
        """
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(name="Alice")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                return list(Post.objects.order_by("id").select_related("author"))

        result = orm.schema(query=Query, optimizer=False).execute_sync(
            "{ posts { title author { name } } }", context_value={}
        )
        assert result.errors is None, result.errors
        assert [row["author"]["name"] for row in result.data["posts"]] == [
            "Alice",
            "Alice",
            "Bob",
            "Charlie",
        ]
