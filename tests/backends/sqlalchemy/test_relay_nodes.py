"""A relay ``node()`` field answers a row ``scope_rows`` hides as a missing one.

``nodes(ids:)`` answers each ID in its own position. A hidden row was dropped,
moving every later answer up one, while an ID with no row stayed ``null``, so
comparing the two told a client which hidden rows exist.
"""

import pytest
import strawberry
from sqlalchemy import select
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post, User


def _gid(pk: int) -> str:
    return str(relay.GlobalID("PostNode", str(pk)))


def _schema(optimizer: bool):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )

    @orm.type(Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.where(Post.is_published.is_(True))

        @classmethod
        def resolve_nodes(cls, *, info, node_ids, required=False):
            wanted = [int(pk) for pk in node_ids]
            statement = select(Post).where(Post.id.in_(wanted))
            rows = {row.id: row for row in info.context["session"].scalars(statement)}
            return [rows.get(pk) for pk in wanted]

    @strawberry.type
    class Query:
        nodes: list[relay.Node] = relay.node()
        maybe_nodes: list[relay.Node | None] = relay.node()

    return orm.schema(query=Query, optimizer=optimizer, types=[PostNode])


def _ask(schema, session, field: str, *pks: int):
    ids = ", ".join(f'"{_gid(pk)}"' for pk in pks)
    result = schema.execute_sync(
        "{ " + field + "(ids: [" + ids + "]) { ... on PostNode { title } } }",
        context_value={"session": session},
    )
    return result.data, [error.message for error in result.errors or ()]


@pytest.mark.parametrize("optimizer", [True, False])
def test_a_hidden_row_holds_its_place_as_null(sa_session, seed, optimizer):
    schema = _schema(optimizer)
    assert _ask(schema, sa_session, "maybeNodes", 1, 3, 99, 2) == (
        {
            "maybeNodes": [
                {"title": "Hello World"},
                None,
                None,
                {"title": "GraphQL Guide"},
            ]
        },
        [],
    )


@pytest.mark.parametrize("optimizer", [True, False])
def test_a_required_hidden_row_fails_as_a_missing_one_does(sa_session, seed, optimizer):
    schema = _schema(optimizer)
    hidden = _ask(schema, sa_session, "nodes", 1, 3)
    assert hidden == _ask(schema, sa_session, "nodes", 1, 99)
    assert hidden[1], hidden


def test_the_cast_node_leaves_on_a_row_does_not_type_it_elsewhere(sa_session, seed):
    """``node()`` casts the row it returns, and the session hands that row on.

    The cast names a type ``Profile`` cannot return, so a row two types under
    ``Profile`` read is still refused rather than given to either.
    """
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )

    @strawberry.interface
    class Profile:
        name: str

    @orm.type(User, name="StaffProfile")
    class StaffProfile(Profile):
        name: auto
        email: auto

    @orm.type(User, name="PublicProfile", exclude=["email"])
    class PublicProfile(Profile):
        name: auto

    @orm.type(User, name="UserNode", exclude=["email"])
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def resolve_nodes(cls, *, info, node_ids, required=False):
            rows = [info.context["session"].get(User, int(pk)) for pk in node_ids]
            info.context["held"] = rows
            return rows

    @strawberry.type
    class Query:
        node: relay.Node | None = relay.node()

        @strawberry.field
        def people(self, info: strawberry.Info) -> list[Profile | None]:
            statement = select(User).where(User.id == 1)
            return list(info.context["session"].scalars(statement))

    schema = orm.schema(query=Query, types=[StaffProfile, PublicProfile, UserNode])
    node_id = str(relay.GlobalID("UserNode", "1"))
    result = schema.execute_sync(
        '{ node(id: "' + node_id + '") { id } '
        "people { __typename ... on StaffProfile { email } } }",
        context_value={"session": sa_session},
    )
    assert result.data == {"node": {"id": node_id}, "people": [None]}
    assert [error.path for error in result.errors or ()] == [["people", 0]]
    assert "strawberry.cast" in result.errors[0].message
