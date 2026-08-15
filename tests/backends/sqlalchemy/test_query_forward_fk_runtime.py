"""Forward-FK query shapes that exercise nested author traversal paths."""

import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryForwardFKRuntime:
    def _build_schema(self, User, Post, *, author_scope_rows=None):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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

    def test_root_posts_can_select_forward_fk(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name }
                }
            }
            """,
            context_value={"session": sa_session},
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
        self, sa_session, seed, User, Post
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
            """,
            context_value={"session": sa_session},
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

    def test_forward_fk_respects_type_level_scope_rows(
        self, sa_session, seed, User, Post
    ):
        schema = self._build_schema(
            User,
            Post,
            author_scope_rows=lambda qs, info: qs.filter(User.email.contains("@")),
        )
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name email }
                }
            }
            """,
            context_value={"session": sa_session},
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
