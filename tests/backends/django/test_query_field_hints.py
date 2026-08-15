"""Field hints tests — registration from abstract, integration backend-specific."""

import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm.types import auto
from tests.abstract.hint_validation import AbstractTestHintValidation
from tests.abstract.query_field_hints import AbstractTestQueryFieldHintsRegistration


class TestQueryFieldHintsRegistration(AbstractTestQueryFieldHintsRegistration):
    pass


class TestHintValidation(AbstractTestHintValidation):
    pass


class TestQueryFieldHintsIntegration:
    def test_load_hint_eager_loads_relationship(self, orm, seed, Post, Tag):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field.auto(using=["author"])

        @strawberry.type
        class Q:
            @strawberry.field
            def posts(self) -> list[PT]:
                return Post.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ posts { title tags { name } } }")
        assert result.errors is None
        assert result.data == {
            "posts": [
                {"title": "Hello World", "tags": [{"name": "python"}]},
                {
                    "title": "GraphQL Guide",
                    "tags": [{"name": "python"}, {"name": "graphql"}],
                },
                {"title": "Draft Post", "tags": []},
                {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
            ]
        }

    def test_disable_optimization_skips_field(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto = orm.field.auto(disable_optimization=True)

        hints = orm.backend._store.get("UT", "name")
        assert hints.disable_optimization is True


class TestComputedFieldHintIntegration:
    def test_computed_field_hint_eager_loads_relation(self, orm, seed, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return f"by {self.author.name}"

        @strawberry.type
        class Q:
            @strawberry.field
            def posts(self) -> list[PT]:
                return Post.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ posts { title byline } }")

        assert result.errors is None
        assert [row["byline"] for row in result.data["posts"]] == [
            "by Alice",
            "by Alice",
            "by Bob",
            "by Charlie",
        ]
        assert len(ctx) == 1

    def test_computed_field_hint_applies_under_nested_relation(
        self, orm, seed, Post, User
    ):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return f"by {self.author.name}"

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
            result = schema.execute_sync("{ users { name posts { title byline } } }")

        assert result.errors is None
        assert result.data["users"][0]["posts"][0]["byline"] == "by Alice"
        assert len(ctx) <= 3

    def test_unknown_computed_hint_is_ignored(self, orm_factory, seed, Post):
        """With strict_hints off, an unresolvable hint degrades instead of raising."""
        orm = orm_factory(strict_hints=False)

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["does_not_exist"])
            def shout(self) -> str:
                return self.title.upper()

        @strawberry.type
        class Q:
            @strawberry.field
            def posts(self) -> list[PT]:
                return Post.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ posts { shout } }")

        assert result.errors is None
        assert result.data["posts"][0]["shout"] == "HELLO WORLD"


class TestRelationKindHints:
    """Reverse and many-to-many names are relations too, not just forward FKs."""

    def test_reverse_relation_is_a_valid_hint(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["comments"])
            def comment_count(self) -> int:
                return 0

        assert orm.backend._store.get("PT", "comment_count").using == ["comments"]

    def test_many_to_many_is_a_valid_hint(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["tags"])
            def tag_count(self) -> int:
                return 0

        assert orm.backend._store.get("PT", "tag_count").using == ["tags"]
