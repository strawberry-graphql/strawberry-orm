"""Which fields skip the library's schema extensions under ``orm.schema()``."""

from types import SimpleNamespace

import pytest
import strawberry
from graphql import GraphQLBoolean, GraphQLField, GraphQLObjectType
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.leaf_fields import _attribute_read, _leaf_resolvers
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def _leaves_by_type(schema, orm):
    leaves = _leaf_resolvers(schema._schema, orm._backend)
    return {
        name: {
            field
            for field, value in graphql_type.fields.items()
            if value.resolve in leaves
        }
        for name, graphql_type in schema._schema.type_map.items()
        if isinstance(graphql_type, GraphQLObjectType) and not name.startswith("__")
    }


@pytest.mark.django_db
def test_only_columns_and_the_relay_id_skip_the_hooks(monkeypatch, User):
    monkeypatch.setattr(
        User, "shout", property(lambda user: user.name.upper()), raising=False
    )
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        shout: str

    @strawberry.type
    class Stats:
        count: int

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection()

        @strawberry.field
        def stats(self) -> Stats:
            return Stats(count=1)

    leaves = _leaves_by_type(orm.schema(query=Query), orm)

    assert leaves["UserNode"] == {"id", "name"}
    assert leaves["PageInfo"] == {
        "hasNextPage",
        "hasPreviousPage",
        "startCursor",
        "endCursor",
    }
    assert leaves["UserNodeEdge"] == {"cursor"}
    assert leaves["Stats"] == set()
    assert leaves["Query"] == set()


@pytest.mark.django_db
def test_an_app_subclass_of_a_library_extension_still_runs_on_columns(seed, User):
    orm = StrawberryORM.for_django(warn_missing_scope=False)
    seen = []

    class Watching(orm.optimizer_extension()):
        def resolve(self, _next, root, info, *args, **kwargs):
            seen.append(info.field_name)
            return super().resolve(_next, root, info, *args, **kwargs)

    @orm.type(User)
    class UserType:
        id: auto
        name: auto

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self) -> list[UserType]:
            return User.objects.order_by("id")

    result = orm.schema(query=Query, extensions=[Watching]).execute_sync(
        "{ users { name } }", context_value={}
    )

    assert result.errors is None, result.errors
    assert seen.count("name") == len(result.data["users"]) > 0


def test_a_field_strawberry_did_not_define_is_not_a_leaf():
    page_info = GraphQLObjectType(
        "PageInfo",
        {"hasNextPage": GraphQLField(GraphQLBoolean, resolve=lambda *_: True)},
        extensions={"strawberry-definition": SimpleNamespace(origin=relay.PageInfo)},
    )
    schema = SimpleNamespace(type_map={"PageInfo": page_info})

    assert _leaf_resolvers(schema, backend=None) == frozenset()


def test_a_relay_id_whose_attribute_cannot_be_found_is_not_a_leaf():
    class Unresolvable:
        @classmethod
        def resolve_id_attr(cls):
            raise AttributeError("no NodeID")

    field = SimpleNamespace(
        base_resolver=SimpleNamespace(wrapped_func=None), _orm_relay_id=True
    )
    assert _attribute_read(Unresolvable, field) is None


@pytest.mark.django_db
def test_a_relay_id_read_off_a_property_is_not_a_leaf(monkeypatch, User):
    monkeypatch.setattr(
        User, "handle", property(lambda user: user.name.lower()), raising=False
    )
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(User)
    class UserNode(relay.Node):
        handle: relay.NodeID[str]
        name: auto

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection()

    assert _leaves_by_type(orm.schema(query=Query), orm)["UserNode"] == {"name"}
