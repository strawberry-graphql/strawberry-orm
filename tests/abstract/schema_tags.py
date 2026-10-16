"""Tagging fields with the schemas they belong to, and building one per tag.

A backend provides ``orm`` (a fresh ORM), ``models`` (``User`` and ``Post``)
and ``run(schema, query)``, which executes against the seeded rows. Building
the types runs each backend's own relation handling, which is where a tag can
be lost, so every way of tagging is resolved here, not just printed.
"""

import strawberry
from strawberry import relay

from strawberry_orm import auto, tags
from strawberry_orm.relay import ORMListConnection

PUBLIC = "public"
INTERNAL = "internal"
PARTNER = "partner"
ALICE_POSTS = ["GraphQL Guide", "Hello World"]


def build_query(orm, models):
    @orm.type(models.User, name="Author")
    class AuthorType:
        id: auto
        name: auto

    @orm.type(models.Comment)
    class CommentType:
        id: auto
        body: auto

    @orm.type(models.Post)
    class PostType:
        id: auto
        title: auto
        body: auto = orm.field.eager(tags=INTERNAL)
        author: AuthorType = strawberry.field(metadata=tags(INTERNAL))

    @orm.type(models.User)
    class UserType:
        id: auto
        name: auto
        email: auto = strawberry.field(metadata=tags(INTERNAL))
        posts: list[PostType] = orm.field.eager(tags=INTERNAL)
        comments: list[CommentType] = orm.field.eager(
            disable_optimization=True, tags=INTERNAL
        )

        @orm.field.computed(tags=[INTERNAL, PARTNER])
        def shout(self, info: strawberry.Info) -> str:
            return self.name.upper()

    @orm.type(models.User, name="UserNode")
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.eager()
        users_page: ORMListConnection[UserNode] = orm.connection.eager(tags=INTERNAL)

        @strawberry.field(metadata=tags(INTERNAL))
        def audit(self) -> str:
            return "ok"

    return Query


def _users(data):
    return sorted(data["users"], key=lambda user: user["name"])


class AbstractTestSchemaTags:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def schema(self, orm, models, tag=None):
        query = build_query(orm, models)
        return (
            orm.schema(query=query)
            if tag is None
            else orm.schema(query=query, tags=tag)
        )

    # -- What each schema contains ---------------------------------------------

    def test_untagged_fields_are_in_every_schema(self, orm, models):
        for tag in (PUBLIC, INTERNAL):
            sdl = str(self.schema(orm, models, tag))
            assert "users: [UserType!]!" in sdl
            assert "  name: String!" in sdl

    def test_tagged_fields_are_left_out_of_other_schemas(self, orm, models):
        sdl = str(self.schema(orm, models, PUBLIC))
        for name in ("email", "posts", "comments", "shout", "audit", "usersPage"):
            assert f"{name}:" not in sdl and f"{name}(" not in sdl, name

    def test_a_field_is_in_each_schema_it_is_tagged_for(self, orm, models):
        partner = str(self.schema(orm, models, PARTNER))
        assert "shout: String!" in partner
        assert "email" not in partner

    def test_a_type_reached_only_through_hidden_fields_is_left_out(self, orm, models):
        sdl = str(self.schema(orm, models, PUBLIC))
        for type_name in ("PostType", "Author", "UserNode", "CommentType"):
            assert f"type {type_name} " not in sdl, type_name

    def test_a_schema_without_tags_keeps_every_field(self, orm, models):
        sdl = str(self.schema(orm, models))
        for name in ("email", "posts", "comments", "shout", "audit", "usersPage"):
            assert f"{name}" in sdl, name

    def test_metadata_reaches_the_field_alongside_tags(self, orm, models):
        @orm.type(models.User)
        class Owned:
            id: auto
            name: auto = orm.field.eager(tags=INTERNAL, metadata={"owner": "team"})

        [name] = [
            field
            for field in Owned.__strawberry_definition__.fields
            if field.python_name == "name"
        ]
        assert name.metadata["owner"] == "team"
        assert name.metadata["strawberry_orm.tags"] == frozenset({INTERNAL})

    # -- Resolving what a schema keeps -----------------------------------------

    async def test_tagged_columns_resolve_in_their_schema(self, orm, models, seed):
        data = await self.execute(
            self.schema(orm, models, INTERNAL), "{ users { name email shout } }"
        )
        assert _users(data)[0] == {
            "name": "Alice",
            "email": "alice@example.com",
            "shout": "ALICE",
        }

    async def test_tagged_relations_resolve_in_their_schema(self, orm, models, seed):
        data = await self.execute(
            self.schema(orm, models, INTERNAL),
            "{ users { name posts { title body author { name } } comments { body } } }",
        )
        alice = _users(data)[0]
        assert sorted(post["title"] for post in alice["posts"]) == ALICE_POSTS
        assert [comment["body"] for comment in alice["comments"]] == ["Thanks!"]
        assert {post["author"]["name"] for post in alice["posts"]} == {"Alice"}

    async def test_a_tagged_connection_resolves_in_its_schema(self, orm, models, seed):
        data = await self.execute(
            self.schema(orm, models, INTERNAL),
            "{ usersPage(first: 1) { edges { node { name } } } }",
        )
        assert len(data["usersPage"]["edges"]) == 1

    async def test_a_hidden_field_cannot_be_queried(self, orm, models, seed):
        result = await self.run(
            self.schema(orm, models, PUBLIC), "{ users { name email } }"
        )
        assert "Cannot query field 'email'" in result.errors[0].message
