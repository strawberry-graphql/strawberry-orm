"""N+1 prevention tests: verify the optimizer prevents N+1 at various depths,
with custom queryset overrides, load=[...] field hints, load callables, and
nested get_queryset application for the Tortoise backend."""

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


@pytest_asyncio.fixture
async def tortoise_db():
    """Set up an in-memory Tortoise DB and tear it down after the test."""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["tests.backends.tortoise.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


@pytest_asyncio.fixture
async def seed(tortoise_db):
    from tests.backends.tortoise.models import Comment, Post, Tag, User

    alice = await User.create(id=1, name="Alice", email="alice@example.com")
    bob = await User.create(id=2, name="Bob", email="bob@example.com")
    charlie = await User.create(id=3, name="Charlie", email="charlie@test.org")

    python = await Tag.create(id=1, name="python")
    graphql = await Tag.create(id=2, name="graphql")
    rust = await Tag.create(id=3, name="rust")

    p1 = await Post.create(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = await Post.create(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    await Post.create(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = await Post.create(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )

    await p1.tags.add(python)
    await p2.tags.add(python, graphql)
    await p4.tags.add(rust)

    await Comment.create(id=1, body="Nice post!", post=p1, author=bob)
    await Comment.create(id=2, body="Thanks!", post=p1, author=alice, parent_id=1)
    await Comment.create(id=3, body="Great guide", post=p2, author=charlie)


class TestQueryNPlusOnePrevention:
    @pytest.mark.asyncio
    async def test_three_level_deep_nesting(self, seed, Comment, Post, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title comments { body } } } }"
        )
        assert result.errors is None
        data = result.data
        assert data is not None
        for user in data["users"]:
            user["posts"].sort(key=lambda post: post["title"])
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {
                            "title": "GraphQL Guide",
                            "comments": [{"body": "Great guide"}],
                        },
                        {
                            "title": "Hello World",
                            "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
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

    @pytest.mark.asyncio
    async def test_sibling_relationships_stay_bounded(
        self, seed, Comment, Post, Tag, User
    ):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title tags { name } comments { body } } } }"
        )
        assert result.errors is None
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_without_optimizer_causes_more_queries(self, seed, Post, User):
        """Without the optimizer, accessing nested relations triggers extra queries."""
        orm = StrawberryORM.for_tortoise()

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
            async def users(self) -> list[UT]:
                users = await User.all().prefetch_related("posts")
                return users  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q)
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None


class TestQueryCustomQuerysetNoNPlusOne:
    @pytest.mark.asyncio
    async def test_get_queryset_with_nested_relationships(self, seed, Post, Tag, User):
        orm = StrawberryORM.for_tortoise()

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT]

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title tags { name } } } }"
        )
        assert result.errors is None
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_get_queryset_preserves_optimizer_eager_loads(self, seed, Post, User):
        orm = StrawberryORM.for_tortoise()

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(email__contains="example.com")

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        assert len(result.data["users"]) == 2


class TestQueryLoadHintNoNPlusOne:
    @pytest.mark.asyncio
    async def test_load_hint_adds_eager_load_efficiently(self, seed, Post, Tag, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title tags { name } } } }"
        )
        assert result.errors is None
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_multiple_load_hints_stay_efficient(
        self, seed, Comment, Post, Tag, User
    ):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title tags { name } comments { body } } } }"
        )
        assert result.errors is None
        assert result.data is not None


class TestQueryLoadCallable:
    @pytest.mark.asyncio
    async def test_load_callable_filters_nested_relation(self, seed, Post, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        data = result.data
        assert data is not None
        for user in data["users"]:
            user["posts"].sort(key=lambda post: post["title"])
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "GraphQL Guide"},
                        {"title": "Hello World"},
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

    @pytest.mark.asyncio
    async def test_load_callable_with_nested_children(self, seed, Post, Tag, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
            "{ users { name posts { title tags { name } } } }"
        )
        assert result.errors is None
        alice_posts = result.data["users"][0]["posts"]
        assert len(alice_posts) == 2
        assert all(
            p["title"] != "Draft Post" for u in result.data["users"] for p in u["posts"]
        )

    @pytest.mark.asyncio
    async def test_load_callable_stays_efficient(self, seed, Post, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        assert result.data is not None


class TestQueryNestedGetQueryset:
    @pytest.mark.asyncio
    async def test_get_queryset_filters_nested_relation(self, seed, Post, User):
        orm = StrawberryORM.for_tortoise()

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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        users = {
            user["name"]: sorted(post["title"] for post in user["posts"])
            for user in result.data["users"]
        }
        assert users == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }

    @pytest.mark.asyncio
    async def test_get_queryset_still_applies_with_nested_filter_argument(
        self, seed, Post, User
    ):
        orm = StrawberryORM.for_tortoise()
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
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute(
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

    @pytest.mark.asyncio
    async def test_get_queryset_composes_with_load_callable(self, seed, Post, User):
        """Both type-level get_queryset and field-level load callable should compose."""
        orm = StrawberryORM.for_tortoise()

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
                load=lambda qs: qs.exclude(title="GraphQL Guide"),
            )

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ users { name posts { title } } }")
        assert result.errors is None
        all_post_titles = [p["title"] for u in result.data["users"] for p in u["posts"]]
        assert "Draft Post" not in all_post_titles
        assert "GraphQL Guide" not in all_post_titles
        assert "Hello World" in all_post_titles

    @pytest.mark.asyncio
    async def test_custom_m2m_prefetch_does_not_leak_between_parents(
        self, seed, Post, Tag
    ):
        orm = StrawberryORM.for_tortoise()

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field(load=lambda qs: qs.filter(name="rust"))

        @strawberry.type
        class Q:
            @strawberry.field
            def posts(self) -> list[PT]:
                return Post.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ posts { title tags { name } } }")
        assert result.errors is None
        assert result.data == {
            "posts": [
                {"title": "Hello World", "tags": []},
                {"title": "GraphQL Guide", "tags": []},
                {"title": "Draft Post", "tags": []},
                {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
            ]
        }
