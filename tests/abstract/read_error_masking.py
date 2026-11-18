"""A read that fails tells the client one plain message, whatever failed.

The ORM's and the driver's errors name tables, columns, validators and the
values they refused, the SQL, and sometimes the model's Python class. So
whatever a read raises from that family, and wherever it raises it, the client
is told "Invalid filter expression" (or "Database connection failed" when
there is no database to ask), and the original is kept as the cause, for the
server log.

Each family is raised two ways: while the read runs a statement (``running``,
the driver or the ORM failing as it executes), and while the read builds one
(``building``, the type's ``scope_rows`` failing as it narrows the rows). A
plain ``ValueError`` or ``TypeError`` is one of the ORM's too when it raises
it running a statement - a field preparing a value, a driver binding it - so
those are raised as statements run as well.

Only a backend's own error handling tells its families apart, and every read
reaches it the same way, so each family is raised on one read, and each read
is given the backend's ``TYPICAL`` families. The plain Python errors are
raised on every read, as code along one read's way could catch one of them
(a ``getattr`` default, a ``KeyError`` taken for a missing key).

A backend provides ``orm`` and ``models`` (``User``, ``Post``, ``Comment``,
seeded with at least one commented post) and, on the test class:

- ``FAMILIES``: ``{name: exception class}``, each built with one message;
- ``TYPICAL``: the names of two families, a database error and one of the
  ORM's own;
- ``EXECUTIONS``: the ways a schema runs, such as ``("sync", "async")``;
- ``run(schema, query, execution, variables=None)``, an awaitable result;
- ``failing(error)``, a context manager under which every statement raises
  *error*.
"""

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

#: What an error's own message gives away, none of which may reach a client.
LEAK = (
    "DzLeak: column dz_secret_ledger.tenant_code (ValidationError on "
    "DzSecretModel) in SELECT * FROM dz_secret_ledger WHERE x = 'acme-0042'"
)
PLAIN = {"Invalid filter expression", "Database connection failed"}

#: Errors of no family of their own, raised while the ORM builds or runs a
#: statement: by a field preparing a value, or by the driver binding it.
RUNNING = {
    "AttributeError": AttributeError,
    "KeyError": KeyError,
    "TypeError": TypeError,
    "UnicodeEncodeError": lambda message: UnicodeEncodeError(
        "utf-8", message, 0, 1, "surrogates not allowed"
    ),
    "ValueError": ValueError,
}

ENTRY_POINTS = {
    "root list": "{ posts { title } }",
    "root filter": '{ posts(filter: {field: {title: {exact: "x"}}}) { title } }',
    "root order": "{ posts(order: [{field: {title: DESC}}]) { title } }",
    "connection": "{ postConn(first: 5) { totalCount edges { node { title } } } }",
    "aggregates": "{ postConn { aggregates { count max { wordCount } } } }",
    "groups": (
        "{ postConn(groupBy: [{field: {title: true}}]) {"
        " groups { key { title } aggregates { count } } } }"
    ),
    "relation field": "{ users { name posts { title } } }",
    "relation connection": (
        "{ userNodes { name posts(first: 5) { totalCount edges { node { title } } } } }"
    ),
    "to-one relation": "{ comments { body post { title } } }",
    "filter through a relation": (
        '{ users(filter: {object: {posts: {field: {title: {exact: "x"}}}}}) { name } }'
    ),
}


#: Reads given a value through ``$title``.
FILTERED_BY_VARIABLE = {
    "root filter": (
        "query ($title: String!) {"
        " posts(filter: {field: {title: {exact: $title}}}) { title } }"
    ),
    "connection filter": (
        "query ($title: String!) {"
        " postConn(filter: {field: {title: {exact: $title}}}) { totalCount } }"
    ),
    "filter through a relation": (
        "query ($title: String!) {"
        " users(filter: {object: {posts: {field: {title: {exact: $title}}}}})"
        " { name } }"
    ),
}


def causes(error):
    """*error* and what it was raised from, a driver's error wrapped included."""
    found = []
    while error is not None and error not in found:
        found.append(error)
        error = error.__cause__ or getattr(error, "orig", None) or error.__context__
    return found


#: The read each family is raised on.
EVERY_FAMILY_ON = "root list"


class AbstractTestReadErrorMasking:
    FAMILIES: dict = {}
    TYPICAL: tuple = ()
    EXECUTIONS: tuple = ("async",)

    def pytest_generate_tests(self, metafunc):
        if "family" in metafunc.fixturenames:
            metafunc.parametrize("family", sorted(self.FAMILIES))
        if "typical" in metafunc.fixturenames:
            metafunc.parametrize("typical", self.TYPICAL)
        if "running" in metafunc.fixturenames:
            metafunc.parametrize("running", sorted({*self.TYPICAL, *RUNNING}))
        if "entry" in metafunc.fixturenames:
            metafunc.parametrize("entry", sorted(ENTRY_POINTS))
        if "execution" in metafunc.fixturenames:
            metafunc.parametrize("execution", self.EXECUTIONS)

    def schema(self, orm, models, scope):
        User, Post, Comment = models.User, models.Post, models.Comment

        @orm.type(
            Post,
            filters=orm.filter(Post),
            order=orm.order(Post),
            group=orm.group(Post),
            aggregate=orm.aggregate(Post),
        )
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            word_count: auto

            @classmethod
            def scope_rows(cls, query, info):
                return scope(query)

        @orm.type(Comment)
        class CommentType:
            body: auto
            post: PostNode | None

        @orm.type(User, filters=orm.filter(User), name="Author")
        class Author:
            name: auto
            posts: list[PostNode]

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            posts: list[PostNode] = orm.field.eager()
            post_conn: ORMListConnection[PostNode] = orm.connection.eager()
            comments: list[CommentType] = orm.field.eager()
            users: list[Author] = orm.field.eager()
            user_nodes: list[UserNode] = orm.field.eager()

        return orm.schema(query=Query)

    def assert_plain(self, result, error):
        assert result.errors, result.data
        for found in result.errors:
            assert found.message in PLAIN, found.message
            assert error in causes(found.original_error), repr(found.original_error)

    async def failing_statement(self, orm, models, error, entry, execution):
        schema = self.schema(orm, models, lambda query: query)
        with self.failing(error):
            result = await self.run(schema, ENTRY_POINTS[entry], execution)
        self.assert_plain(result, error)

    async def failing_scope(self, orm, models, error, entry, execution):
        def scope(query):
            raise error

        schema = self.schema(orm, models, scope)
        result = await self.run(schema, ENTRY_POINTS[entry], execution)
        self.assert_plain(result, error)

    async def test_a_statement_failing_as_it_runs(
        self, orm, models, running, entry, execution
    ):
        error = {**self.FAMILIES, **RUNNING}[running](LEAK)
        await self.failing_statement(orm, models, error, entry, execution)

    async def test_each_family_failing_as_a_statement_runs(
        self, orm, models, family, execution
    ):
        error = self.FAMILIES[family](LEAK)
        await self.failing_statement(orm, models, error, EVERY_FAMILY_ON, execution)

    async def test_a_scope_failing_as_it_builds_one(
        self, orm, models, typical, entry, execution
    ):
        error = self.FAMILIES[typical](LEAK)
        await self.failing_scope(orm, models, error, entry, execution)

    async def test_each_family_failing_as_a_scope_builds_one(
        self, orm, models, family, execution
    ):
        error = self.FAMILIES[family](LEAK)
        await self.failing_scope(orm, models, error, EVERY_FAMILY_ON, execution)

    async def test_a_value_the_driver_cannot_bind(self, orm, models, execution):
        """A lone surrogate, which JSON variables carry and UTF-8 cannot."""
        schema = self.schema(orm, models, lambda query: query)
        for entry, query in FILTERED_BY_VARIABLE.items():
            result = await self.run(
                schema, query, execution, variables={"title": "\ud800"}
            )
            assert result.errors, (entry, result.data)
            for found in result.errors:
                assert found.message in PLAIN, (entry, found.message)

    async def test_a_read_that_does_not_fail_is_answered(self, orm, models, execution):
        schema = self.schema(orm, models, lambda query: query)
        for entry, query in ENTRY_POINTS.items():
            result = await self.run(schema, query, execution)
            assert result.errors is None, (entry, result.errors)
