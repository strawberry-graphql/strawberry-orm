"""Field hints tests — registration from abstract, integration backend-specific."""

import strawberry

from strawberry_orm.types import auto
from tests.abstract.query_field_hints import AbstractTestQueryFieldHintsRegistration


class TestQueryFieldHintsRegistration(AbstractTestQueryFieldHintsRegistration):
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
            tags: list[TT] = orm.field(load=["author"])

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
            name: auto = orm.field(disable_optimization=True)

        hints = orm.backend._store.get("UT", "name")
        assert hints.disable_optimization is True
