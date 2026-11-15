"""An app's own ``resolve`` extension runs on every field, scalars included.

The library keeps its own hooks off a field that only reads a column; the
app's hooks are not the library's to skip.
"""

from collections import Counter

import strawberry
from strawberry import relay
from strawberry.extensions import SchemaExtension

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class AbstractTestAppResolveExtensions:
    QUERY = "{ users { edges { node { id name posts { title } } } } }"

    def schema(self, orm, User, Post, seen):
        class Record(SchemaExtension):
            def resolve(self, _next, root, info, *args, **kwargs):
                seen.append(info.field_name)
                return _next(root, info, *args, **kwargs)

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        return orm.schema(query=Query, extensions=[Record])

    def _assert_every_field_was_seen(self, result, seen):
        assert result.errors is None, result.errors
        assert len(result.data["users"]["edges"]) == 3
        counts = Counter(seen)
        assert (counts["id"], counts["name"], counts["posts"], counts["title"]) == (
            3,
            3,
            3,
            4,
        )

    def test_an_apps_resolve_extension_sees_every_field(
        self, orm, seed, User, Post, schema_execute
    ):
        seen = []
        result = schema_execute(self.schema(orm, User, Post, seen), self.QUERY)
        self._assert_every_field_was_seen(result, seen)


class AbstractTestAppResolveExtensionsAsync(AbstractTestAppResolveExtensions):
    async def test_an_apps_resolve_extension_sees_every_field(
        self, orm, seed, User, Post, schema_execute
    ):
        seen = []
        result = await schema_execute(self.schema(orm, User, Post, seen), self.QUERY)
        self._assert_every_field_was_seen(result, seen)
