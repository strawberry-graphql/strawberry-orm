"""Forward-FK query shapes that exercise nested author traversal paths."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryForwardFKRuntime:
    def _build_schema(self, User, Post, *, author_scope_rows=None):
        orm = StrawberryORM.for_tortoise()

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

    @pytest.mark.asyncio
    async def test_root_posts_can_select_forward_fk(self, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
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

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="Tortoise nested forward-FK traversal under prefetched reverse relations is still broken",
    )
    async def test_prefetched_reverse_relations_can_select_nested_forward_fk(
        self, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
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

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="Tortoise custom forward-FK queryset prefetch is still broken",
    )
    async def test_forward_fk_respects_type_level_scope_rows(self, seed, User, Post):
        schema = self._build_schema(
            User,
            Post,
            author_scope_rows=lambda qs, info: qs.filter(email__contains="@"),
        )
        result = await schema.execute(
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
