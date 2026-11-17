"""A fragment spread more than once is walked once.

graphql-core collects a fragment once per selection set however often it is
spread, so ``author { ...U1 ...U1 ...U1 }`` executes as ``author { ...U1 }``
does. The optimizer followed every spread, and planned the fields below each
one again, so a document repeating its spreads at every level took time
exponential in its depth to plan what one spread needs. A connection had
Strawberry convert its whole selection the same way, optimizer or not.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``
and ``Tag``), and on the test class ``run(schema, query)``, executing against
the seeded rows and recording the number of SQL statements in
``self.query_count``.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures". Tags: python on the first two,
graphql on the second, rust on the last.
"""

from collections import Counter

import pytest
import strawberry
from strawberry import relay
from strawberry.types.nodes import SelectedField

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

#: Fragments below the root, each spreading the next.
LEVELS = 4
#: How often each fragment spreads the next in the repeated document.
REPEATS = 6


def _document(root: str, on: str, repeats: int) -> str:
    """*root* spreading ``U0``, and each ``U<n>`` spreading ``U<n+1>`` *repeats* times."""
    fragments = []
    for level in range(LEVELS):
        below = (
            " ".join([f"...U{level + 1}"] * repeats) if level + 1 < LEVELS else "name"
        )
        fragments.append(
            f"fragment U{level} on {on} {{ name posts {{ title author {{ {below} }} }} }}"
        )
    return "\n".join([f"query {{ {root} }}", *fragments])


def _pages_document(repeats: int) -> str:
    """Each user's page of posts, its fragments each spread *repeats* times."""
    posts = " ".join(["...PostFields"] * repeats)
    users = " ".join(["...UserPages"] * repeats)
    return f"""
    fragment PostFields on PostNode {{ title tags {{ name }} }}
    fragment UserPages on UserNode {{
      name posts(first: 5) {{ totalCount edges {{ node {{ {posts} }} }} }}
    }}
    query {{ users {{ {users} }} }}
    """


def _list_schema(orm, models):
    @orm.type(models.Tag)
    class TagType:
        id: auto
        name: auto

    class PostType:
        id: auto
        title: auto
        is_published: auto
        tags: list[TagType]

    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

    # Set here, as types local to a function cannot be named in a string.
    PostType.__annotations__["author"] = UserType
    post_inputs = {"filters": orm.filter(models.Post), "order": orm.order(models.Post)}
    orm.type(models.Post, **post_inputs)(PostType)
    orm.type(models.User)(UserType)

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.eager()

    return orm.schema(query=Query)


def _connection_schema(orm, models):
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: list[PostNode]

    PostNode.__annotations__["author"] = UserNode
    orm.type(models.Post)(PostNode)
    orm.type(models.User)(UserNode)

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _relation_connection_schema(orm, models):
    # A relation connection takes its arguments from a node type registered
    # before the parent's class body, so these types cannot loop back.
    @orm.type(models.Tag)
    class TagNode:
        id: auto
        name: auto

    @orm.type(models.Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]

    @orm.type(models.User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    @strawberry.type
    class Query:
        users: list[UserNode] = orm.field.eager()

    return orm.schema(query=Query)


class AbstractTestRepeatedFragments:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    @pytest.fixture
    def work(self, orm, monkeypatch):
        """The fields the optimizer plans, and the ones Strawberry converts."""
        counts: Counter[str] = Counter()
        resolves_itself = orm.backend.resolves_itself
        from_node = SelectedField.from_node.__func__

        def planned(type_name, field_name):
            counts["planned"] += 1
            return resolves_itself(type_name, field_name)

        def converted(cls, info, node):
            counts["converted"] += 1
            return from_node(cls, info, node)

        monkeypatch.setattr(orm.backend, "resolves_itself", planned)
        monkeypatch.setattr(SelectedField, "from_node", classmethod(converted))
        return counts

    async def _once_and_repeated(self, schema, work, document):
        """The data, statements and work of *document* spreading once, then often."""
        runs = []
        for repeats in (1, REPEATS):
            work.clear()
            data = await self.execute(schema, document(repeats))
            runs.append((data, self.query_count, work.copy()))
        return runs

    async def test_repeated_spreads_plan_what_one_spread_does(self, orm, models, work):
        schema = _list_schema(orm, models)
        once, repeated = await self._once_and_repeated(
            schema,
            work,
            lambda repeats: _document("users { ...U0 }", "UserType", repeats),
        )
        assert repeated[0] == once[0]
        assert repeated[1] == once[1], (repeated[1], once[1])
        assert repeated[2]["planned"] == once[2]["planned"], (repeated[2], once[2])

    async def test_a_connection_reads_repeated_spreads_once(self, orm, models, work):
        schema = _connection_schema(orm, models)
        root = "users(first: 5) { totalCount edges { node { ...U0 } } }"
        once, repeated = await self._once_and_repeated(
            schema, work, lambda repeats: _document(root, "UserNode", repeats)
        )
        assert repeated[0] == once[0]
        assert repeated[1] == once[1], (repeated[1], once[1])
        assert repeated[2]["converted"] == once[2]["converted"], (repeated[2], once[2])
        assert repeated[2]["planned"] == once[2]["planned"], (repeated[2], once[2])

    async def test_a_relation_connection_reads_repeated_spreads_once(
        self, orm, models, work
    ):
        schema = _relation_connection_schema(orm, models)
        once, repeated = await self._once_and_repeated(schema, work, _pages_document)
        assert repeated[0] == once[0]
        assert repeated[1] == once[1], (repeated[1], once[1])
        assert repeated[2]["converted"] == once[2]["converted"], (repeated[2], once[2])
        assert repeated[2]["planned"] == once[2]["planned"], (repeated[2], once[2])

    async def test_overlapping_and_inline_fragments_load_like_the_plain_query(
        self, orm, models
    ):
        schema = _list_schema(orm, models)
        plain = await self.execute(
            schema,
            "{ users { name posts { title tags { name } author { name posts { title } } } } }",
        )
        plain_statements = self.query_count
        fragmented = await self.execute(
            schema,
            """
            fragment Name on UserType { name }
            fragment Titles on PostType { title }
            fragment Tagged on PostType { ...Titles tags { name } }
            fragment Author on PostType { author { ...Name posts { ...Titles } } }
            fragment Posts on UserType {
              posts {
                ...Tagged
                ...Author
                ... on PostType { ...Titles author { ...Name } }
                ...Tagged
              }
            }
            query { users { ...Name ...Posts ... on UserType { ...Posts name } ...Name } }
            """,
        )
        assert fragmented == plain
        assert self.query_count == plain_statements

    async def test_aliases_of_one_relation_keep_their_own_arguments(self, orm, models):
        schema = _list_schema(orm, models)
        data = await self.execute(
            schema,
            """
            fragment Published on UserType {
              published: posts(filter: { field: { isPublished: { exact: true } } }) {
                title
              }
            }
            fragment Drafts on UserType {
              drafts: posts(filter: { field: { isPublished: { exact: false } } }) {
                title
              }
            }
            fragment ByTitle on UserType {
              byTitle: posts(order: [{ field: { title: ASC } }]) { title }
            }
            query {
              users {
                name ...Published ...Drafts ...Published ...ByTitle
                ...Drafts posts { title } ...ByTitle
              }
            }
            """,
        )

        def titles(*names):
            return [{"title": name} for name in names]

        assert data["users"] == [
            {
                "name": "Alice",
                "published": titles("Hello World", "GraphQL Guide"),
                "drafts": [],
                "byTitle": titles("GraphQL Guide", "Hello World"),
                "posts": titles("Hello World", "GraphQL Guide"),
            },
            {
                "name": "Bob",
                "published": [],
                "drafts": titles("Draft Post"),
                "byTitle": titles("Draft Post"),
                "posts": titles("Draft Post"),
            },
            {
                "name": "Charlie",
                "published": titles("Rust Adventures"),
                "drafts": [],
                "byTitle": titles("Rust Adventures"),
                "posts": titles("Rust Adventures"),
            },
        ]

    async def test_aliases_of_a_relation_connection_keep_their_own_pages(
        self, orm, models
    ):
        schema = _relation_connection_schema(orm, models)
        data = await self.execute(
            schema,
            """
            fragment One on UserNode { one: posts(first: 1) { edges { node { title } } } }
            fragment Two on UserNode {
              two: posts(first: 2) { totalCount edges { node { title } } }
            }
            query { users { name ...One ...Two ...One ...Two } }
            """,
        )

        def page(*names):
            return [{"node": {"title": name}} for name in names]

        assert data["users"] == [
            {
                "name": "Alice",
                "one": {"edges": page("Hello World")},
                "two": {"totalCount": 2, "edges": page("Hello World", "GraphQL Guide")},
            },
            {
                "name": "Bob",
                "one": {"edges": page("Draft Post")},
                "two": {"totalCount": 1, "edges": page("Draft Post")},
            },
            {
                "name": "Charlie",
                "one": {"edges": page("Rust Adventures")},
                "two": {"totalCount": 1, "edges": page("Rust Adventures")},
            },
        ]
