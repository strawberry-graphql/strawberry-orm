"""``orm.connection`` split by whether the callable sees the parent row.

``eager`` is handed no parent, so one query can serve every caller and a
``scope`` can narrow it. ``lazy`` receives ``self`` and therefore runs once per
parent. The declaration-time checks here exist because both of the mistakes
they catch used to surface as a broken field at query time instead.
"""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def _orm():
    return StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="off")


@pytest.mark.django_db
class TestConnectionNamespace:
    def _nodes(self, orm, User, Post):
        @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(User, filters=orm.filter(User), order=orm.order(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        return UserNode, PostNode

    def _titles(self, schema, query):
        result = schema.execute_sync(query, context_value={})
        assert result.errors is None, result.errors
        return result.data

    def test_eager_bare_builds_the_connection(self, seed, User, Post):
        orm = _orm()
        UserNode, _ = self._nodes(orm, User, Post)

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection.eager()

        data = self._titles(
            orm.schema(query=Query), "{ users(first: 10) { edges { node { name } } } }"
        )
        assert [e["node"]["name"] for e in data["users"]["edges"]] == [
            "Alice",
            "Bob",
            "Charlie",
        ]

    def test_eager_applies_its_scope(self, seed, User, Post):
        """A scope on a connection has to narrow it, as it does everywhere else."""
        orm = _orm()
        UserNode, _ = self._nodes(orm, User, Post)

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection.eager(
                scope=lambda qs, info: qs.filter(name="Alice")
            )

        data = self._titles(
            orm.schema(query=Query), "{ users(first: 10) { edges { node { name } } } }"
        )
        assert [e["node"]["name"] for e in data["users"]["edges"]] == ["Alice"]

    def test_scope_also_narrows_total_count(self, seed, User, Post):
        """totalCount comes off the stashed query, so it must see the scope too."""
        orm = _orm()
        UserNode, _ = self._nodes(orm, User, Post)

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection.eager(
                scope=lambda qs, info: qs.filter(name="Alice")
            )

        data = self._titles(
            orm.schema(query=Query), "{ users(first: 10) { totalCount } }"
        )
        assert data["users"]["totalCount"] == 1

    def test_lazy_receives_the_parent_row(self, seed, User, Post):
        orm = _orm()
        _, PostNode = self._nodes(orm, User, Post)

        @orm.type(User)
        class UserNode2(relay.Node):
            id: relay.NodeID[int]
            name: auto

            @orm.connection.lazy(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> list[PostNode]:
                return self.posts.all()

        @strawberry.type
        class Query:
            users: list[UserNode2] = orm.field.auto()

        data = self._titles(
            orm.schema(query=Query),
            "{ users { name posts(first: 10) { edges { node { title } } } } }",
        )
        by_user = {
            u["name"]: sorted(e["node"]["title"] for e in u["posts"]["edges"])
            for u in data["users"]
        }
        assert by_user["Bob"] == ["Draft Post"]

    # -- rejected shapes -----------------------------------------------------

    def test_unknown_keyword_is_rejected(self):
        """These used to be swallowed, so a misspelled scope silently did nothing."""
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            _orm().connection(nonsense=1)

    def test_scope_with_a_resolver_signature_is_rejected(self):
        with pytest.raises(TypeError, match="never sees the parent row"):
            _orm().connection.eager(scope=lambda self, info: self)

    def test_lazy_rejects_a_scope_signature(self):
        with pytest.raises(TypeError, match="must take self"):
            _orm().connection.lazy(resolver=lambda qs, info: qs)

    def test_a_connection_naming_no_relation_is_rejected(self, User, Post):
        """On a type it is served by a relation, so it has to name one."""
        orm = _orm()
        _, PostNode = self._nodes(orm, User, Post)

        with pytest.raises(ValueError, match="has no relation 'postz'"):

            @orm.type(User)
            class UserNode3(relay.Node):
                id: relay.NodeID[int]
                postz: ORMListConnection[PostNode] = orm.connection.eager()
