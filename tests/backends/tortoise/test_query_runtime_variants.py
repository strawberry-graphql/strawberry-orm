"""Runtime schema variants that exercise public field/connection branches."""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class TestQueryRuntimeVariants:
    def _build_schema(self, User, Post):
        orm = StrawberryORM.for_tortoise()
        UserFilter = orm.filter(User)
        UserOrder = orm.order(User)
        PostFilter = orm.filter(Post)
        PostOrder = orm.order(Post)

        @orm.type(Post, filters=PostFilter)
        class FilterablePost:
            id: auto
            title: auto
            is_published: auto

        @orm.type(Post, order=PostOrder)
        class OrderablePost:
            id: auto
            title: auto

        @orm.type(User, filters=UserFilter)
        class FilterOnlyUser:
            id: auto
            name: auto

        @orm.type(User, order=UserOrder)
        class OrderOnlyUser:
            id: auto
            name: auto

        @orm.type(User)
        class PlainUser:
            id: auto
            name: auto

        @orm.type(User)
        class UserWithFilteredPosts:
            id: auto
            name: auto
            posts: list[FilterablePost]

        @orm.type(User)
        class UserWithOrderedPosts:
            id: auto
            name: auto
            posts: list[OrderablePost]

        @orm.type(User, filters=UserFilter, order=UserOrder)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @strawberry.type
        class Query:
            filter_only_users: list[FilterOnlyUser] = orm.field()
            order_only_users: list[OrderOnlyUser] = orm.field()
            plain_users: list[PlainUser] = orm.field()
            users_with_filtered_posts: list[UserWithFilteredPosts] = orm.field()
            users_with_ordered_posts: list[UserWithOrderedPosts] = orm.field()
            users_connection = orm.connection(ORMListConnection[UserNode])

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    @pytest.mark.asyncio
    async def test_root_auto_fields_support_plain_filter_and_order_variants(
        self, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
            """
            {
                filterOnlyUsers(filter: { field: { name: { exact: "Alice" } } }) {
                    name
                }
                orderOnlyUsers(order: [{ field: { name: DESC } }]) {
                    name
                }
                plainUsers {
                    name
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "filterOnlyUsers": [{"name": "Alice"}],
            "orderOnlyUsers": [
                {"name": "Charlie"},
                {"name": "Bob"},
                {"name": "Alice"},
            ],
            "plainUsers": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ],
        }

    @pytest.mark.asyncio
    async def test_nested_relations_support_filter_only_and_order_only_variants(
        self, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
            """
            {
                usersWithFilteredPosts {
                    name
                    posts(filter: { field: { isPublished: { exact: true } } }) {
                        title
                    }
                }
                usersWithOrderedPosts {
                    name
                    posts(order: [{ field: { title: DESC } }]) {
                        title
                    }
                }
            }
            """
        )
        assert result.errors is None
        filtered = {
            user["name"]: sorted(post["title"] for post in user["posts"])
            for user in result.data["usersWithFilteredPosts"]
        }
        ordered = {
            user["name"]: [post["title"] for post in user["posts"]]
            for user in result.data["usersWithOrderedPosts"]
        }
        assert filtered == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }
        assert ordered == {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": ["Draft Post"],
            "Charlie": ["Rust Adventures"],
        }

    @pytest.mark.asyncio
    async def test_nested_relations_without_arguments_use_prefetched_values(
        self, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
            """
            {
                usersWithFilteredPosts {
                    name
                    posts { title }
                }
                usersWithOrderedPosts {
                    name
                    posts { title }
                }
            }
            """
        )
        assert result.errors is None
        filtered = {
            user["name"]: sorted(post["title"] for post in user["posts"])
            for user in result.data["usersWithFilteredPosts"]
        }
        ordered = {
            user["name"]: sorted(post["title"] for post in user["posts"])
            for user in result.data["usersWithOrderedPosts"]
        }
        assert filtered == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": ["Draft Post"],
            "Charlie": ["Rust Adventures"],
        }
        assert ordered == filtered

    @pytest.mark.asyncio
    async def test_descriptor_connection_uses_annotation_type(self, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = await schema.execute(
            """
            {
                usersConnection(order: [{ field: { name: DESC } }], first: 2) {
                    edges {
                        node { name }
                    }
                    pageInfo { hasNextPage }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "usersConnection": {
                "edges": [
                    {"node": {"name": "Charlie"}},
                    {"node": {"name": "Bob"}},
                ],
                "pageInfo": {"hasNextPage": True},
            }
        }
