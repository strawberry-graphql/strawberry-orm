"""Generated node inputs write only the relations the type lets them write.

Inputs from ``orm.mutations`` leave out the columns a type's ``exclude=``
hides. A relation writes too: a link sets its foreign key column, and a list
adds rows to the relation. So a relation the type excludes, or a link whose
foreign key column it excludes, is left out as well, and a ``project=`` naming
one is refused. A relation with a sensitive-looking name, such as ``roles``,
is left out unless a ``project=`` names it, as ``password_hash`` is left out
unless ``_meta`` lists it.

Backends supply ``orm`` and ``models``: the blog's ``User``, ``Post``,
``Comment`` and ``Tag``, and an ``Account`` whose ``roles`` reach ``Role``.
Nothing here writes; the schema is the evidence.
"""

import re
from typing import Any

import pytest
import strawberry
from graphql import get_named_type
from strawberry import relay

from strawberry_orm.types import auto

POST_COLUMNS = {
    "title": "String",
    "body": "String",
    "isPublished": "Boolean",
    "createdAt": "DateTime",
    "wordCount": "Int",
}


def _schema(**inputs: type) -> strawberry.Schema:
    """A schema with one mutation per input, named by its keyword."""

    def field_for(input_type: type) -> Any:
        def resolver(input: Any) -> bool:
            return True

        resolver.__annotations__ = {"input": input_type, "return": bool}
        return strawberry.field(resolver=resolver)

    namespace = {name: field_for(input_type) for name, input_type in inputs.items()}
    mutation = strawberry.type(type("Mutation", (), namespace))
    query = strawberry.type(
        type("Query", (), {"__annotations__": {"ok": bool}, "ok": True})
    )
    return strawberry.Schema(query=query, mutation=mutation)


def shape(schema: strawberry.Schema, type_name: str) -> dict[str, str]:
    """Every field of the input *type_name*, with its type as the SDL prints it."""
    fields = schema._schema.type_map[type_name].fields
    return {name: str(field.type) for name, field in fields.items()}


def branch(schema: strawberry.Schema, root: str, field: str) -> str:
    """The name of the input type *root*'s *field* takes."""
    return get_named_type(schema._schema.type_map[root].fields[field].type).name


def errors(schema: strawberry.Schema, query: str) -> list[str]:
    return [error.message for error in schema.execute_sync(query).errors]


def hide_authorship(orm: Any, m: Any, excluded: list[str]) -> None:
    @orm.type(m.Post, exclude=excluded)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        body: auto


def hide_posts(orm: Any, m: Any) -> None:
    @orm.type(m.User, exclude=["posts"])
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto


def refused(model: str, relation: str, excluded: list[str]) -> str:
    return re.escape(
        f"The project for {model} names the relation '{relation}', but the "
        f"GraphQL type for {model} excludes {excluded!r}."
    )


class AbstractTestExcludedRelations:
    def test_an_excluded_link_is_left_out_of_every_input(self, orm, models):
        hide_authorship(orm, models, ["author", "author_id"])
        schema = _schema(
            create=orm.mutations.create_node_input(
                models=[models.Post, models.User], name="CreateNode"
            ),
            update=orm.mutations.update_node_input(
                models=[models.Post], name="UpdateNode"
            ),
        )
        lists = {"tags": "[PostTagsRef!]", "comments": "[PostCommentsRef!]"}
        assert shape(schema, "CreatePostNodeInput") == {**POST_COLUMNS, **lists}
        assert shape(schema, "UpdatePostNodeInput") == {
            **POST_COLUMNS,
            "id": "ID!",
            **lists,
        }
        # The same inputs are what a user's posts are created and updated from.
        assert shape(schema, "UserPostsRef") == {
            "create": "CreatePostNodeInput",
            "update": "UpdatePostNodeInput",
        }
        assert errors(
            schema,
            'mutation { update(input: {post: {id: "1", author: {update: {id: "2"}}}}) }',
        ) == [
            "Expected value of type 'UpdatePostNodeInput' not to include unknown "
            'field \'author\', found: { id: "1", author: { update: { id: "2" } } }.'
        ]

    def test_a_link_whose_foreign_key_column_is_excluded_is_left_out(self, orm, models):
        hide_authorship(orm, models, ["author_id"])
        schema = _schema(
            create=orm.mutations.create_node_input(
                models=[models.Post], name="CreateNode"
            )
        )
        assert shape(schema, "CreatePostNodeInput") == {
            **POST_COLUMNS,
            "tags": "[PostTagsRef!]",
            "comments": "[PostCommentsRef!]",
        }

    def test_an_excluded_list_is_left_out(self, orm, models):
        hide_posts(orm, models)
        schema = _schema(
            update=orm.mutations.update_node_input(
                models=[models.User], name="UpdateNode"
            )
        )
        assert shape(schema, "UpdateUserNodeInput") == {
            "name": "String",
            "email": "String",
            "createdAt": "DateTime",
            "id": "ID!",
            "comments": "[UserCommentsRef!]",
        }

    @pytest.mark.parametrize("excluded", [["author"], ["author_id"]])
    def test_a_projection_cannot_name_an_excluded_link(self, orm, models, excluded):
        hide_authorship(orm, models, excluded)
        with pytest.raises(ValueError, match=refused("Post", "author", excluded)):
            orm.mutations.upsert_node_input(
                model=models.Post,
                project={
                    "_meta": {
                        "create": ["title", "body"],
                        "upsert": {"where": ["title"]},
                    },
                    "author": {"_meta": {"upsert": {"where": ["id"]}}},
                },
            )
        with pytest.raises(ValueError, match=refused("Post", "author", excluded)):
            orm.mutations.create_node_input(
                models=[models.User], project={"user": {"posts": {"author": {}}}}
            )

    def test_a_projection_cannot_name_an_excluded_list(self, orm, models):
        hide_posts(orm, models)
        with pytest.raises(ValueError, match=refused("User", "posts", ["posts"])):
            orm.mutations.update_node_input(
                models=[models.User], project={"user": {"posts": {}}}
            )

    @pytest.mark.parametrize("excluded", [["author"], ["author_id"]])
    def test_declaring_the_type_after_an_input_that_writes_the_link_is_refused(
        self, orm, models, excluded
    ):
        orm.mutations.upsert_node_input(
            model=models.Post,
            project={
                "_meta": {"name": "EarlyPost", "upsert": {"where": ["title"]}},
                "author": {"_meta": {"upsert": {"where": ["id"]}}},
            },
        )
        written = re.escape(
            f"PostNode excludes {excluded!r} but EarlyPostCreate can still write them"
        )
        with pytest.raises(ValueError, match=written):
            hide_authorship(orm, models, excluded)

    def test_declaring_the_type_after_an_input_that_writes_the_list_is_refused(
        self, orm, models
    ):
        orm.mutations.upsert_node_input(
            model=models.User,
            project={
                "_meta": {"name": "EarlyUser", "upsert": {"where": ["name"]}},
                "posts": {},
            },
        )
        written = re.escape(
            "UserNode excludes ['posts'] but EarlyUserCreate can still write them"
        )
        with pytest.raises(ValueError, match=written):
            hide_posts(orm, models)


def account_node(orm: Any, m: Any) -> None:
    @orm.type(m.Account)
    class AccountNode(relay.Node):
        id: relay.NodeID[int]
        name: auto


class AbstractTestSensitiveRelations:
    def test_a_sensitive_looking_relation_is_left_out(self, orm, models):
        account_node(orm, models)
        schema = _schema(
            update=orm.mutations.update_node_input(
                models=[models.Account], name="UpdateNode"
            ),
            # A root the project does not name gets its relations one level deep.
            create=orm.mutations.create_node_input(
                models=[models.Account, models.Role],
                project={"role": {}},
                name="CreateNode",
            ),
        )
        assert shape(schema, "UpdateAccountNodeInput") == {
            "name": "String",
            "pinned": "Boolean",
            "id": "ID!",
        }
        assert shape(schema, branch(schema, "CreateNode", "account")) == {
            "name": "String",
            "pinned": "Boolean",
        }
        assert errors(
            schema,
            'mutation { update(input: {account: {id: "7", roles: [{update: {id: "1"}}]}}) }',
        ) == [
            "Expected value of type 'UpdateAccountNodeInput' not to include unknown "
            'field \'roles\', found: { id: "7", roles: [{ update: { id: "1" } }] }.'
        ]

    def test_a_projection_naming_it_keeps_it(self, orm, models):
        account_node(orm, models)
        schema = _schema(
            update=orm.mutations.update_node_input(
                models=[models.Account],
                project={"account": {"_meta": {"name": "Admin"}, "roles": {}}},
                name="UpdateNode",
            )
        )
        assert shape(schema, "AdminUpdate") == {
            "name": "String",
            "pinned": "Boolean",
            "id": "ID!",
            "roles": "[AdminRolesItem!]",
        }


class AbstractTestPlainInputKeys:
    """``orm.input()`` and ``orm.partial()`` leave out a link's foreign key
    column, as node inputs do: written as a plain value, it would link a row
    the related repo's ``scope_query`` and ``can_link`` never see."""

    def test_the_foreign_key_a_link_sets_is_left_out(self, orm, models):
        schema = _schema(create=orm.input(models.Post), patch=orm.partial(models.Post))
        assert shape(schema, "PostInput") == POST_COLUMNS
        assert shape(schema, "PostPartialInput") == POST_COLUMNS

    def test_include_naming_it_keeps_it(self, orm, models):
        schema = _schema(
            create=orm.input(models.Post, include=["title", "author_id"]),
            patch=orm.partial(models.Post, include=["author_id"]),
        )
        assert shape(schema, "PostInput") == {"title": "String", "authorId": "Int"}
        assert shape(schema, "PostPartialInput") == {"authorId": "Int"}
