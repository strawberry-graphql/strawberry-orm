"""Field hints tests — registration from abstract, integration backend-specific."""

import pytest
import strawberry

from strawberry_orm.types import auto
from tests.abstract.hint_validation import AbstractTestHintValidation


class TestHintValidation(AbstractTestHintValidation):
    pass


class TestRelationKindHints:
    """Tortoise only materialises reverse relations during ``Tortoise.init``.

    Forward FKs are visible on an uninitialised model, so a typo is still
    rejected at import time, but a reverse name such as ``comments`` can only
    be validated once the ORM has been initialised.
    """

    @pytest.mark.asyncio
    async def test_relation_names_include_forward_and_reverse_after_init(
        self, orm, seed, Post
    ):
        names = orm.backend.relation_names(Post)
        assert "author" in names  # forward FK, known even before init
        assert "comments" in names  # reverse FK, only after init
        assert "tags" in names  # many-to-many
        assert "title" not in names  # a column is not a relation

    @pytest.mark.asyncio
    async def test_reverse_hint_is_accepted_after_init(self, orm, seed, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["comments"])
            def comment_count(self) -> int:
                return 0

        assert orm.backend._store.get("PT", "comment_count").using == ["comments"]

    @pytest.mark.asyncio
    async def test_many_to_many_hint_is_accepted_after_init(self, orm, seed, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["tags"])
            def tag_count(self) -> int:
                return 0

        assert orm.backend._store.get("PT", "tag_count").using == ["tags"]


class TestComputedFieldHintIntegration:
    @pytest.mark.asyncio
    async def test_computed_field_hint_prefetches_relation(self, orm, seed, Post):
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
                return Post.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = await schema.execute("{ posts { title byline } }")

        assert result.errors is None
        assert [row["byline"] for row in result.data["posts"]] == [
            "by Alice",
            "by Alice",
            "by Bob",
            "by Charlie",
        ]

    @pytest.mark.asyncio
    async def test_computed_field_hint_under_scoped_relation_keeps_scoping(
        self, orm, seed, Post, User
    ):
        """A hint below a scoped relation must not add an unscoped prefetch."""

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(is_published=True)

            @orm.field.computed(using=["author"])
            def shout(self) -> str:
                return self.title.upper()

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
        result = await schema.execute("{ users { name posts { title shout } } }")

        assert result.errors is None
        titles = {
            row["title"] for user in result.data["users"] for row in user["posts"]
        }
        assert "Draft Post" not in titles
        assert result.data["users"][0]["posts"][0]["shout"] == "HELLO WORLD"
