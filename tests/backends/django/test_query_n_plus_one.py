"""N+1 prevention tests: verify the optimizer prevents N+1 at various depths,
with custom queryset overrides, load=[...] field hints, load callables, and
nested get_queryset application."""

import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryNPlusOnePrevention:
    def test_three_level_deep_nesting(self, orm, seed, Comment, Post, User):
        @orm.type(Comment)
        class CT:
            id: auto
            body: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            comments: list[CT]

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title comments { body } } } }"
            )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {
                            "title": "Hello World",
                            "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                        },
                        {
                            "title": "GraphQL Guide",
                            "comments": [{"body": "Great guide"}],
                        },
                    ],
                },
                {
                    "name": "Bob",
                    "posts": [
                        {"title": "Draft Post", "comments": []},
                    ],
                },
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures", "comments": []},
                    ],
                },
            ]
        }
        assert len(ctx) <= 5

    def test_sibling_relationships_stay_bounded(
        self, orm, seed, Comment, Post, Tag, User
    ):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Comment)
        class CT:
            id: auto
            body: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT]
            comments: list[CT]

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } comments { body } } } }"
            )
        assert result.errors is None
        assert result.data is not None
        assert len(ctx) <= 6

    def test_without_optimizer_causes_more_queries(self, orm, seed, Post, User):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q)
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
        assert result.errors is None
        assert len(ctx) > 2


class TestQueryCustomQuerysetNoNPlusOne:
    def test_get_queryset_with_nested_relationships(self, seed, Post, Tag, User):
        qs_orm = StrawberryORM.for_django()

        @qs_orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @qs_orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT]

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(is_published=True)

        @qs_orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[qs_orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } } } }"
            )
        assert result.errors is None
        assert result.data is not None
        assert len(ctx) <= 6

    def test_get_queryset_preserves_optimizer_eager_loads(self, seed, Post, User):
        qs_orm = StrawberryORM.for_django()

        @qs_orm.type(Post)
        class PT:
            id: auto
            title: auto

        @qs_orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(email__endswith="example.com")

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[qs_orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
        assert result.errors is None
        assert len(ctx) <= 3
        assert len(result.data["users"]) == 2


class TestQueryLoadHintNoNPlusOne:
    def test_load_hint_adds_eager_load_efficiently(self, orm, seed, Post, Tag, User):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field(load=["author"])

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } } } }"
            )
        assert result.errors is None
        assert result.data is not None
        assert len(ctx) <= 6

    def test_multiple_load_hints_stay_efficient(
        self, orm, seed, Comment, Post, Tag, User
    ):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Comment)
        class CT:
            id: auto
            body: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field(load=["comments"])
            comments: list[CT]

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } comments { body } } } }"
            )
        assert result.errors is None
        assert result.data is not None
        assert len(ctx) <= 7


class TestQueryLoadCallable:
    def test_load_callable_filters_nested_relation(self, seed, Post, User):
        orm = StrawberryORM.for_django()

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(load=lambda qs: qs.filter(is_published=True))

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
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
                {"name": "Bob", "posts": []},
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures"},
                    ],
                },
            ]
        }
        assert len(ctx) <= 3

    def test_load_callable_with_nested_children(self, seed, Post, Tag, User):
        orm = StrawberryORM.for_django()

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT]

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(load=lambda qs: qs.filter(is_published=True))

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } } } }"
            )
        assert result.errors is None
        alice_posts = result.data["users"][0]["posts"]
        assert len(alice_posts) == 2
        assert all(
            p["title"] != "Draft Post" for u in result.data["users"] for p in u["posts"]
        )
        assert len(ctx) <= 5

    def test_load_callable_stays_efficient(self, seed, Post, User):
        orm = StrawberryORM.for_django()

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(load=lambda qs: qs.filter(is_published=True))

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
        assert result.errors is None
        assert len(ctx) <= 3


class TestQueryNestedGetQueryset:
    def test_get_queryset_filters_nested_relation(self, seed, Post, User):
        orm = StrawberryORM.for_django()

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(is_published=True)

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
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
                {"name": "Bob", "posts": []},
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures"},
                    ],
                },
            ]
        }
        assert len(ctx) <= 3

    def test_get_queryset_still_applies_with_nested_filter_argument(
        self, seed, Post, User
    ):
        orm = StrawberryORM.for_django()
        PostFilter = orm.filter(Post)

        @orm.type(Post, filters=PostFilter)
        class PT:
            id: auto
            title: auto
            is_published: auto

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(is_published=True)

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            """
            {
                users {
                    name
                    posts(filter: { field: { title: { contains: "Draft" } } }) {
                        title
                        isPublished
                    }
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "posts": []},
                {"name": "Bob", "posts": []},
                {"name": "Charlie", "posts": []},
            ]
        }

    def test_get_queryset_composes_with_load_callable(self, seed, Post, User):
        """Both type-level get_queryset and field-level load callable should compose."""
        orm = StrawberryORM.for_django()

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(is_published=True)

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(
                load=lambda qs: qs.exclude(title="GraphQL Guide")
            )

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")
        assert result.errors is None
        all_post_titles = [p["title"] for u in result.data["users"] for p in u["posts"]]
        assert "Draft Post" not in all_post_titles
        assert "GraphQL Guide" not in all_post_titles
        assert "Hello World" in all_post_titles
        assert len(ctx) <= 3
