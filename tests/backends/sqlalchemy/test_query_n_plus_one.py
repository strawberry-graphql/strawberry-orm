"""N+1 prevention tests: verify the optimizer prevents N+1 at various depths,
with custom queryset overrides, load=[...] field hints, load callables, and
nested get_queryset application."""

import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryNPlusOnePrevention:
    def test_three_level_deep_nesting(
        self, orm, sa_session, seed, query_counter, Comment, Post, User
    ):
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title comments { body } } } }",
            context_value={"session": sa_session},
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
        assert len(query_counter) <= 3

    def test_sibling_relationships_stay_bounded(
        self, orm, sa_session, seed, query_counter, Comment, Post, Tag, User
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } comments { body } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data is not None
        assert len(query_counter) <= 5

    def test_without_optimizer_causes_more_queries(
        self, orm, sa_session, seed, query_counter, Post, User
    ):
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                session = info.context["session"]
                return session.execute(select(User)).scalars().unique().all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q)
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert len(query_counter) > 2


class TestQueryCustomQuerysetNoNPlusOne:
    def test_get_queryset_with_nested_relationships(
        self, sa_session, seed, query_counter, Post, Tag, User
    ):
        qs_orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published == True)  # noqa: E712

        @qs_orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[qs_orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data is not None
        assert len(query_counter) <= 4

    def test_get_queryset_preserves_optimizer_eager_loads(
        self, sa_session, seed, query_counter, Post, User
    ):
        qs_orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
            def get_queryset(cls, stmt, info):
                return stmt.where(User.email.like("%example.com"))

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[qs_orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert len(query_counter) <= 2
        assert len(result.data["users"]) == 2


class TestQueryLoadHintNoNPlusOne:
    def test_load_hint_adds_eager_load_efficiently(
        self, orm, sa_session, seed, query_counter, Post, Tag, User
    ):
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data is not None
        assert len(query_counter) <= 4

    def test_multiple_load_hints_stay_efficient(
        self, orm, sa_session, seed, query_counter, Comment, Post, Tag, User
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } comments { body } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data is not None
        assert len(query_counter) <= 5


class TestQueryLoadCallable:
    def test_load_callable_filters_nested_relation(
        self, sa_session, seed, query_counter, Post, User
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(load=lambda stmt: stmt.where(Post.is_published))  # noqa: E712

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
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
        assert len(query_counter) <= 2

    def test_load_callable_with_nested_children(
        self, sa_session, seed, query_counter, Post, Tag, User
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

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
            posts: list[PT] = orm.field(load=lambda stmt: stmt.where(Post.is_published))  # noqa: E712

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        alice_posts = result.data["users"][0]["posts"]
        assert len(alice_posts) == 2
        assert all(
            p["title"] != "Draft Post" for u in result.data["users"] for p in u["posts"]
        )
        assert len(query_counter) <= 3

    def test_load_callable_stays_efficient(
        self, sa_session, seed, query_counter, Post, User
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(load=lambda stmt: stmt.where(Post.is_published))  # noqa: E712

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert len(query_counter) <= 2


class TestQueryNestedGetQueryset:
    def test_get_queryset_filters_nested_relation(
        self, sa_session, seed, query_counter, Post, User
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published == True)  # noqa: E712

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
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
        assert len(query_counter) <= 2

    def test_get_queryset_still_applies_with_nested_filter_argument(
        self, sa_session, seed, Post, User
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        PostFilter = orm.filter(Post)

        @orm.type(Post, filters=PostFilter)
        class PT:
            id: auto
            title: auto
            is_published: auto

            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published == True)  # noqa: E712

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

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
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "posts": []},
                {"name": "Bob", "posts": []},
                {"name": "Charlie", "posts": []},
            ]
        }

    def test_get_queryset_composes_with_load_callable(
        self, sa_session, seed, query_counter, Post, User
    ):
        """Both type-level get_queryset and field-level load callable should compose."""
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(Post.is_published == True)  # noqa: E712

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT] = orm.field(
                load=lambda stmt: stmt.where(Post.title != "GraphQL Guide")
            )

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        all_post_titles = [p["title"] for u in result.data["users"] for p in u["posts"]]
        assert "Draft Post" not in all_post_titles
        assert "GraphQL Guide" not in all_post_titles
        assert "Hello World" in all_post_titles
        assert len(query_counter) <= 2
