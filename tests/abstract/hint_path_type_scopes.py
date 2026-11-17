"""A ``using=`` hop that no type can be told for keeps every type's ``scope=``.

A path such as ``author.posts`` declared on a field of ``PostType`` walks
relations the schema need not show. When ``PostType`` has no ``author`` field,
which of the types over ``User`` the authors are read as cannot be told, so
``posts`` on each of them is a candidate and the path loads only the rows every
candidate's ``scope=`` allows. Planned as the first type registered over
``User``, a ``scope=`` on another type's ``posts`` was left out.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``),
and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``read(obj, name)``, reading a relation the way a resolver on that ORM would;
- ``published_only(query)``, keeping the published posts;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob only "Draft Post"
(the one draft), Charlie "Rust Adventures".
"""

import pytest
import strawberry

from strawberry_orm import auto


class AbstractTestHintPathTypeScopes:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def schema(self, orm, models, *, scoped_first, staff_scope=None):
        read, published_only = self.read, self.published_only

        @orm.type(models.Post, name="ListedPost")
        class ListedPostType:
            id: auto
            title: auto

        def staff_user_type():
            @orm.type(models.User, name="StaffUser")
            class StaffUserType:
                id: auto
                name: auto
                posts: list[ListedPostType] = orm.field.auto(scope=staff_scope)

            return StaffUserType

        def public_user_type():
            @orm.type(models.User, name="PublicUser")
            class PublicUserType:
                id: auto
                name: auto
                posts: list[ListedPostType] = orm.field.auto(
                    scope=lambda qs, info: published_only(qs)
                )

            return PublicUserType

        if scoped_first:
            PublicUserType, StaffUserType = public_user_type(), staff_user_type()
        else:
            StaffUserType, PublicUserType = staff_user_type(), public_user_type()

        @orm.type(models.Post)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(using=["author.posts"])
            def by_the_author(self, info: strawberry.Info) -> list[str]:
                return sorted(
                    post.title for post in read(read(self, "author"), "posts")
                )

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.eager()
            staff: list[StaffUserType] = orm.field.eager()
            users: list[PublicUserType] = orm.field.eager()

        return orm.schema(query=Query)

    @pytest.mark.parametrize("scoped_first", [True, False])
    async def test_the_path_loads_what_every_type_over_the_hop_allows(
        self, orm, models, scoped_first
    ):
        schema = self.schema(orm, models, scoped_first=scoped_first)
        data = await self.execute(schema, "{ posts { title byTheAuthor } }")
        assert {post["title"]: post["byTheAuthor"] for post in data["posts"]} == {
            "Hello World": ["GraphQL Guide", "Hello World"],
            "GraphQL Guide": ["GraphQL Guide", "Hello World"],
            "Draft Post": [],
            "Rust Adventures": ["Rust Adventures"],
        }

    @pytest.mark.parametrize("scoped_first", [True, False])
    async def test_each_types_scope_applies(self, orm, models, scoped_first):
        hide = self.hide
        schema = self.schema(
            orm,
            models,
            scoped_first=scoped_first,
            staff_scope=lambda qs, info: hide(qs, models.Post, "title", "Hello World"),
        )
        data = await self.execute(schema, "{ posts { title byTheAuthor } }")
        assert {post["title"]: post["byTheAuthor"] for post in data["posts"]} == {
            "Hello World": ["GraphQL Guide"],
            "GraphQL Guide": ["GraphQL Guide"],
            "Draft Post": [],
            "Rust Adventures": ["Rust Adventures"],
        }
