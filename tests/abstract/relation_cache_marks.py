"""A relation cache is trusted only by a field with the scope it was loaded under.

Two types can show one model, and each can put its own ``scope=`` on the same
relation. A cache one of them filled holds what that one allowed, so a field
of the other, stricter type must read the relation again rather than trust it.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``),
and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob "Draft Post" (the only
draft), Charlie "Rust Adventures".
"""

import strawberry

from strawberry_orm import auto


class AbstractTestRelationCacheMarks:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def schema(self, orm, models):
        hide, Post = self.hide, models.Post

        @orm.type(Post)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return hide(qs, Post, "title", "Rust Adventures")

        @orm.type(models.User)
        class UserType:
            name: auto
            posts: list[PostType] = orm.field.eager(
                scope=lambda qs, info: hide(qs, Post, "is_published", False)
            )

        @orm.type(models.User, name="AuthorStats")
        class AuthorStatsType:
            name: auto

            @orm.field.computed(using=["posts"])
            def counted(self) -> bool:
                return True

            @strawberry.field
            def profile(self) -> UserType:
                return self

        @strawberry.type
        class Query:
            stats: list[AuthorStatsType] = orm.field.eager()

        return orm.schema(query=Query)

    async def test_a_relation_loaded_for_another_types_scope_is_read_again(
        self, orm, models
    ):
        data = await self.execute(
            self.schema(orm, models),
            "{ stats { name counted profile { posts { title } } } }",
        )
        assert {
            row["name"]: sorted(post["title"] for post in row["profile"]["posts"])
            for row in data["stats"]
        } == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [],
            "Charlie": [],
        }
