"""Relay ``node()`` fields in the schemas ``orm.schema()`` builds.

``node()`` gives its field a resolver as a schema converts it and refuses a
field that has one, so the second build that cuts the inputs, a tagged schema
and every later schema built from the same types each convert a copy.
"""

import pytest
import strawberry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from strawberry import relay

from strawberry_orm import StrawberryORM, auto
from tests.backends.sqlalchemy.models import Base, User

NODES = """
query ($id: ID!, $ids: [ID!]!) {
  node(id: $id) { ... on UserNode { name } }
  nodes(ids: $ids) { ... on UserNode { name } }
}
"""

ANSWER = {"node": {"name": "Ada"}, "nodes": [{"name": "Ada"}, {"name": "Bob"}]}


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            User(id=1, name="Ada", email="ada@example.com"),
            User(id=2, name="Bob", email="bob@example.com"),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _query(orm, cut: bool = True):
    """A query with ``node`` and ``nodes``, over a type whose filter, if it has
    one, cuts ``email``."""

    @orm.type(User, **({"filters": orm.filter(User)} if cut else {}))
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def resolve_nodes(cls, *, info, node_ids, required=False):
            return [info.context["session"].get(User, int(pk)) for pk in node_ids]

    @strawberry.type
    class Query:
        node: relay.Node | None = relay.node()
        nodes: list[relay.Node] = relay.node()
        users: list[UserNode] = orm.field.eager()

    return Query


def _node_field(type_: type):
    return next(
        f for f in type_.__strawberry_definition__.fields if f.python_name == "node"
    )


def _answer(schema, session):
    gid = str(relay.GlobalID("UserNode", "1"))
    other = str(relay.GlobalID("UserNode", "2"))
    result = schema.execute_sync(
        NODES,
        variable_values={"id": gid, "ids": [gid, other]},
        context_value={"session": session},
    )
    assert not result.errors, result.errors
    return result.data


@pytest.mark.parametrize("tags", [None, "public"])
def test_a_schema_that_cuts_an_input_answers_node_and_nodes(orm, session, tags):
    Query = _query(orm)
    schema = orm.schema(query=Query, **({"tags": tags} if tags else {}))
    assert "email" not in str(schema)
    assert _answer(schema, session) == ANSWER


@pytest.mark.parametrize("tags", [None, "public"])
@pytest.mark.parametrize("cut", [True, False], ids=["cut", "uncut"])
@pytest.mark.parametrize("plain", [False, True], ids=["orm", "strawberry"])
def test_a_later_schema_from_the_same_types_still_answers_node(
    orm, session, tags, cut, plain
):
    Query = _query(orm, cut)
    first = orm.schema(query=Query, **({"tags": tags} if tags else {}))
    assert _node_field(Query).base_resolver is None
    later = (
        strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])
        if plain
        else orm.schema(query=Query)
    )
    assert _answer(later, session) == ANSWER
    assert _answer(first, session) == ANSWER


def test_node_arguments_read_before_any_schema_are_not_kept(orm, session):
    Query = _query(orm)
    assert _node_field(Query).arguments == []
    assert _answer(orm.schema(query=Query), session) == ANSWER


def test_a_schema_converts_one_copy_of_a_node_field(orm):
    Query = _query(orm)
    schema = orm.schema(query=Query)
    definition = Query.__strawberry_definition__
    first, second = (
        next(f for f in schema.get_fields(definition) if f.python_name == "node")
        for _ in range(2)
    )
    assert first is second
    assert first is not _node_field(Query)
    converted = schema._schema.query_type.fields["node"]
    assert converted.extensions["strawberry-definition"] is first
