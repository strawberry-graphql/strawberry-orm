"""Async execution tests for the Django backend."""

import pytest
import strawberry
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.django.fixtures import main_schema
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import Tag as DjTag
from tests.backends.django.models import User as DjUser


class TestAsyncExecution:
    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_basic_query(self, seed):
        result = await main_schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_ref_list_mutation(self, seed):
        orm = StrawberryORM.for_django()

        @orm.type(DjTag)
        class TagType:
            name: str

        TagRef = orm.ref(DjTag)

        @strawberry.type
        class Query:
            @strawberry.field
            def ping(self) -> str:
                return "pong"

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await sync_to_async(DjPost.objects.get, thread_sensitive=True)(
                    pk=post_id
                )
                await orm.apply_ref_list(post, "tags", tags, info)
                fetched = await sync_to_async(list, thread_sensitive=True)(
                    post.tags.all()
                )
                return fetched  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query, mutation=Mutation)
        result = await schema.execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [{ update: { id: "3" } }]) {
                    name
                }
            }
            """
        )
        assert result.errors is None
        tag_names = sorted(t["name"] for t in result.data["setPostTags"])
        assert "rust" in tag_names

    @pytest.mark.asyncio
    async def test_async_nested_fk_without_custom_resolver(self, seed, orm, User, Post):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field()

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.optimizer_extension()],
        )
        result = await schema.execute("{ posts { title author { name } } }")
        assert result.errors is None
        assert len(result.data["posts"]) == 4
        assert result.data["posts"][0]["author"]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_async_custom_orm_field_resolver(self, seed, orm, User):
        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            @orm.field
            def users(self) -> list[UserType]:
                return list(DjUser.objects.order_by("id"))  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query)
        result = await schema.execute("{ users { name } }")
        assert result.errors is None
        assert len(result.data["users"]) == 3

    @pytest.mark.asyncio
    async def test_async_relay_connection_filter_and_order(self, seed, User):
        from strawberry import relay

        from strawberry_orm.relay import ORMListConnection

        orm = StrawberryORM.for_django(lazy_resolution="off")
        UserFilter = orm.filter(User)
        UserOrder = orm.order(User)

        @orm.type(User, filters=UserFilter, order=UserOrder)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.connection(ORMListConnection[UserNode])
            def users_connection(self) -> list[UserNode]:
                return orm.get_default_queryset(User)

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.optimizer_extension()],
        )
        result = await schema.execute(
            """
            {
                usersConnection(
                    filter: { field: { email: { contains: "example.com" } } }
                    order: [{ field: { name: DESC } }]
                    first: 2
                ) {
                    edges { node { name } }
                    pageInfo { hasNextPage }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data["usersConnection"]["edges"] == [
            {"node": {"name": "Bob"}},
            {"node": {"name": "Alice"}},
        ]

    @pytest.mark.asyncio
    async def test_async_nested_orm_connection_on_orm_type(self, seed):
        """Sync ``@orm.connection`` on ``@orm.type`` under async GraphQL execution."""
        from strawberry import relay

        from strawberry_orm.relay import ORMListConnection
        from tests.backends.django.models import Comment as DjComment
        from tests.backends.django.models import Post as DjPost

        orm = StrawberryORM.for_django(lazy_resolution="off")
        CommentFilter = orm.filter(DjComment)
        CommentOrder = orm.order(DjComment)
        PostFilter = orm.filter(DjPost)
        PostOrder = orm.order(DjPost)

        @orm.type(DjComment, filters=CommentFilter, order=CommentOrder)
        class CommentNode(relay.Node):
            id: relay.NodeID[int]
            body: auto

        @orm.type(DjPost, filters=PostFilter, order=PostOrder)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            @orm.connection(ORMListConnection[CommentNode])
            def comments(self, info: strawberry.types.Info) -> list[CommentNode]:
                return DjComment.objects.filter(post_id=self.id).order_by("id")  # type: ignore[attr-defined]

        @strawberry.type
        class Query:
            @orm.field
            def posts(self) -> list[PostNode]:
                return list(DjPost.objects.order_by("id"))  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.optimizer_extension()],
        )
        result = await schema.execute(
            """
            {
                posts {
                    comments(first: 10) {
                        edges { node { body } }
                    }
                }
            }
            """
        )
        assert result.errors is None
        first_post_edges = result.data["posts"][0]["comments"]["edges"]
        assert len(first_post_edges) >= 1
