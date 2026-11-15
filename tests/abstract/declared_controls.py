"""A control declared on a field holds wherever the field's rows are reached.

A ``scope=``, a permission or ``exclude=`` is written once, on the field or
the type, and has to hold on every path to the rows or column it guards: the
field itself, its connection's counts, aggregates and groups, and the filters,
orders and groups that read through it.

Backends supply:

``orm``                             a fresh ORM
``models``                          ``User``, ``Post``
``narrow.only(query, model, f, v)`` *query* keeping the rows whose *f* is *v*
``run(schema, query, variables)``   awaitable result of running *query*

The seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures".
"""

import pytest
import strawberry
from strawberry import relay
from strawberry.permission import BasePermission, PermissionExtension

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class Deny(BasePermission):
    message = "Denied."

    def has_permission(self, source, info, **kwargs):
        return False


def _published(narrow, Post):
    return lambda query, info: narrow.only(query, Post, "is_published", True)


def _guarded_scope_schema(orm, models, narrow, *, positional: bool):
    User, Post = models.User, models.Post
    published = _published(narrow, Post)
    guarded = (
        orm.field.eager(published, permission_classes=[Deny])
        if positional
        else orm.field.eager(scope=published, permission_classes=[Deny])
    )

    @orm.type(Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: list[PostNode] = guarded

    @strawberry.type
    class Query:
        users: list[UserNode] = orm.field.eager()

    return orm.schema(query=Query)


def _scoped_connection_schema(orm, models, narrow):
    User, Post = models.User, models.Post

    @orm.type(Post, group=orm.group(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager(
            scope=_published(narrow, Post)
        )

    @strawberry.type
    class Query:
        users: list[UserNode] = orm.field.eager()

    return orm.schema(query=Query)


def _guarded_connection_schema(orm, models):
    User, Post = models.User, models.Post

    @orm.type(Post, filters=orm.filter(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @orm.type(User, filters=orm.filter(User), group=orm.group(User))
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager(
            extensions=[PermissionExtension(permissions=[Deny()])]
        )

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _inherited_guard_schema(orm, models):
    User = models.User

    @strawberry.interface
    class HasEmail:
        email: str = strawberry.field(
            extensions=[PermissionExtension(permissions=[Deny()])]
        )

    @orm.type(
        User,
        filters=orm.filter(User),
        order=orm.order(User),
        group=orm.group(User),
    )
    class UserNode(relay.Node, HasEmail):
        id: relay.NodeID[int]
        name: auto

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _assert_refused(result, name: str) -> None:
    assert result.data is None, result.data
    assert result.errors is not None
    assert any(name in error.message for error in result.errors), result.errors


def _by_name(result) -> dict[str, dict]:
    assert result.errors is None, result.errors
    return {user["name"]: user["posts"] for user in result.data["users"]}


class AbstractTestDeclaredControls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("selection", "bob"),
        [
            (
                "posts(first: 10) { totalCount edges { node { title } } }",
                {"totalCount": 0, "edges": []},
            ),
            (
                "posts { aggregates { count } }",
                {"aggregates": {"count": 0}},
            ),
            (
                "posts(groupBy: [{field: {title: true}}]) { groups { key { title } } }",
                {"groups": []},
            ),
        ],
    )
    async def test_a_relation_connections_scope_hides_its_rows(
        self, orm, models, narrow, run, selection, bob
    ):
        schema = _scoped_connection_schema(orm, models, narrow)
        result = await run(schema, "{ users { name " + selection + " } }")
        assert _by_name(result)["Bob"] == bob, result.data

    @pytest.mark.asyncio
    @pytest.mark.parametrize("positional", [False, True])
    async def test_a_scoped_fields_permission_is_checked(
        self, orm, models, narrow, run, positional
    ):
        schema = _guarded_scope_schema(orm, models, narrow, positional=positional)
        result = await run(schema, "{ users { name posts { title } } }")
        assert result.errors is not None, result.data
        assert {error.message for error in result.errors} == {"Denied."}
        assert "Hello World" not in repr(result.data), result.data

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            '(filter: {object: {posts: {field: {title: {exact: "Draft Post"}}}}})',
            "(groupBy: [{object: {posts: {field: {title: true}}}}])",
        ],
    )
    async def test_a_guarded_connection_cannot_be_reached_through(
        self, orm, models, run, arguments
    ):
        schema = _guarded_connection_schema(orm, models)
        result = await run(schema, "{ users" + arguments + " { totalCount } }")
        _assert_refused(result, "posts")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "arguments",
        [
            '(filter: {field: {email: {startsWith: "bob"}}})',
            "(order: [{field: {email: ASC}}])",
            "(groupBy: [{field: {email: true}}])",
        ],
    )
    async def test_a_guarded_inherited_field_cannot_be_read_through(
        self, orm, models, run, arguments
    ):
        schema = _inherited_guard_schema(orm, models)
        result = await run(schema, "{ users" + arguments + " { totalCount } }")
        _assert_refused(result, "email")
