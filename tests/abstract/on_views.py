"""``on=`` views: their rows are the relation's, read once for every parent.

An ``on=`` view reads a relation under a name of its own. Its rows come from the
read that served every parent, already scoped, so the optimizer must not check
and load them again a parent at a time. A field the app resolves itself is not
a relation read, whatever it is called, and is still checked.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``),
and on the test class:

- ``run(schema, query)``, executing against the seeded rows and recording the
  number of SQL statements in ``self.query_count``;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*;
- ``newest_first(qs, model)``, ordering by primary key, highest first;
- ``add_authors(count)``, adding *count* users, each with a published post and
  a draft;
- ``own_posts(user, info)``, a resolver body reading the user's posts itself.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob "Draft Post" (the only
draft), Charlie "Rust Adventures". Tags: python on the first two, graphql on the
second, rust on the last.
"""

import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection


def _names(data):
    return [edge["node"] for edge in data["authors"]["edges"]]


class AbstractTestOnViews:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def authors_schema(self, orm, models, *, hide_title=None):
        hide, newest_first = self.hide, self.newest_first
        Post = models.Post

        def recent_scope(qs, info):
            return newest_first(hide(qs, Post, "is_published", False), Post)

        def drafts_scope(qs, info):
            return hide(qs, Post, "is_published", True)

        @orm.type(models.Tag)
        class TagType:
            id: auto
            name: auto

        post_namespace = {
            "__annotations__": {"id": auto, "title": auto, "tags": list[TagType]}
        }
        if hide_title is not None:

            def scope_rows(cls, qs, info):
                return hide(qs, Post, "title", hide_title)

            post_namespace["scope_rows"] = classmethod(scope_rows)
        PostType = orm.type(Post)(type("PostType", (), post_namespace))

        @orm.type(models.User)
        class AuthorType(relay.Node):
            id: relay.NodeID[int]
            name: auto
            recent: list[PostType] = orm.field.eager(on="posts", scope=recent_scope)
            drafts: list[PostType] = orm.field.eager(on="posts", scope=drafts_scope)

        @strawberry.type
        class Query:
            authors: ORMListConnection[AuthorType] = orm.connection.eager()

        return orm.schema(query=Query)

    async def test_a_view_under_a_connection_is_read_once(self, orm, models):
        schema = self.authors_schema(orm, models)
        query = "{ authors { edges { node { name recent { title tags { name } } } } } }"

        data = await self.execute(schema, query)
        assert _names(data) == [
            {
                "name": "Alice",
                "recent": [
                    {
                        "title": "GraphQL Guide",
                        "tags": [{"name": "python"}, {"name": "graphql"}],
                    },
                    {"title": "Hello World", "tags": [{"name": "python"}]},
                ],
            },
            {"name": "Bob", "recent": []},
            {
                "name": "Charlie",
                "recent": [{"title": "Rust Adventures", "tags": [{"name": "rust"}]}],
            },
        ]
        three = self.query_count
        # The page, every author's view at once, and every view's tags.
        assert three == self.view_statements, three

        await self.add_authors(9)
        data = await self.execute(schema, query)
        nodes = _names(data)
        assert len(nodes) == 12, nodes
        assert nodes[-1]["recent"] == [{"title": "Book 9", "tags": []}]
        assert self.query_count == three, (three, self.query_count)

    async def test_two_views_of_one_relation_keep_their_own_scopes(self, orm, models):
        schema = self.authors_schema(orm, models)
        data = await self.execute(
            schema,
            "{ authors { edges { node { name recent { title } drafts { title } } } } }",
        )
        assert _names(data) == [
            {
                "name": "Alice",
                "recent": [{"title": "GraphQL Guide"}, {"title": "Hello World"}],
                "drafts": [],
            },
            {"name": "Bob", "recent": [], "drafts": [{"title": "Draft Post"}]},
            {"name": "Charlie", "recent": [{"title": "Rust Adventures"}], "drafts": []},
        ]

    async def test_scope_rows_is_applied_by_the_read_not_checked_after(
        self, orm, models
    ):
        query = "{ authors { edges { node { name recent { title } } } } }"
        await self.execute(self.authors_schema(orm, models), query)
        unscoped = self.query_count

        hiding = self.authors_schema(self.fresh_orm(), models, hide_title="Hello World")
        data = await self.execute(hiding, query)
        assert _names(data)[0] == {
            "name": "Alice",
            "recent": [{"title": "GraphQL Guide"}],
        }
        assert self.query_count == unscoped, (unscoped, self.query_count)


class AbstractTestASelfReadFieldIsChecked:
    """A field named after no relation, resolved by the app, is still scoped."""

    async def test_rows_it_read_itself_are_scope_checked(self, orm, models):
        own_posts, hide = self.own_posts, self.hide
        Post = models.Post

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return hide(qs, Post, "title", "Hello World")

        @orm.type(models.User)
        class AuthorType:
            id: auto
            name: auto

            @strawberry.field
            def writing(self, info: strawberry.Info) -> list[PostType]:
                return own_posts(self, info)

        @strawberry.type
        class Query:
            authors: list[AuthorType] = orm.field.eager()

        result = await self.run(
            orm.schema(query=Query), "{ authors { name writing { title } } }"
        )
        assert result.errors is None, result.errors
        assert result.data["authors"][0] == {
            "name": "Alice",
            "writing": [{"title": "GraphQL Guide"}],
        }
