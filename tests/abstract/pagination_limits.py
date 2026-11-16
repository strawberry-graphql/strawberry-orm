"""How much one connection or list request can make the database read.

A page is held to ``max_results`` however it is asked for: ``before`` alone
and ``last: 0`` read one page, not every row, and a cursor before the first
row is refused rather than read as Python would slice a list from its end.
``default_query_limit`` caps each page and each root list, not the rows a
connection can page through. Groups are capped by ``max_groups``, and the
related rows one ``objects:`` group key holds by ``max_group_key_rows``. The
``order`` and ``groupBy`` lists are capped by ``max_order_entries`` and
``max_group_by_entries`` before any SQL is built.

Backends supply:

``orm_factory(**options)``          an ORM built with *options*
``User``, ``Post``                  the models
``many_users``                      seeds twelve users; their names in id order
``capture()``                       context manager: ``.statements`` sent, and
                                    ``.loaded``, the ``User`` rows built
``run(schema, query)``              awaitable result of running *query*

The seed: Alice wrote posts 1 and 2, Bob post 3, Charlie post 4.
"""

import base64
from typing import Any

import pytest
import strawberry
from graphql import GraphQLInputObjectType, GraphQLObjectType, get_named_type
from strawberry import relay

from strawberry_orm.relay import ORMConnection, ORMListConnection
from strawberry_orm.types import auto


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def _schema(orm, User, Post, *, max_results=None):
    @orm.type(Post, order=orm.order(Post), group=orm.group(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @orm.type(
        User, filters=orm.filter(User), order=orm.order(User), group=orm.group(User)
    )
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: list[PostNode]

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager(
            max_results=max_results
        )
        user_list: list[UserNode] = orm.field.eager()

    return orm.schema(query=Query)


def _nested_schema(orm, User, Post, *, max_results=None):
    @orm.type(Post)
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager(
            max_results=max_results
        )

    @strawberry.type
    class Query:
        user_list: list[UserNode] = orm.field.eager()

    return orm.schema(query=Query)


def _nested_posts(arguments: str) -> str:
    return (
        "{ userList { name posts%s { edges { node { title } } "
        "pageInfo { hasNextPage } } } }" % (f"({arguments})" if arguments else "")
    )


def _post_pages(result) -> dict[str, tuple[list[str], bool]]:
    assert result.errors is None, result.errors
    return {
        user["name"]: (
            [edge["node"]["title"] for edge in user["posts"]["edges"]],
            user["posts"]["pageInfo"]["hasNextPage"],
        )
        for user in result.data["userList"]
    }


def _page(arguments: str) -> str:
    return (
        "{ users%s { totalCount edges { node { name } } "
        "pageInfo { hasPreviousPage hasNextPage } } }"
        % (f"({arguments})" if arguments else "")
    )


def _names(result) -> list[str]:
    assert result.errors is None, result.errors
    return [edge["node"]["name"] for edge in result.data["users"]["edges"]]


def _flags(result) -> tuple[bool, bool]:
    info = result.data["users"]["pageInfo"]
    return info["hasPreviousPage"], info["hasNextPage"]


def _grouped(path: str) -> str:
    return (
        "{ users(first: 1, groupBy: [" + path + "]) "
        "{ groups { aggregates { count } } } }"
    )


def _group_items(arguments: str) -> str:
    """Users 4 to 12 wrote no posts, so they share one group of nine."""
    return (
        "{ users(first: 1, groupBy: [{objects: {posts: {field: {title: true}}}}]) "
        "{ groups { aggregates { count } items"
        + (f"({arguments})" if arguments else "")
        + " { edges { node { name } } pageInfo { hasPreviousPage hasNextPage } } "
        "} } }"
    )


def _aliased_group_items(*pages: tuple[str, str]) -> str:
    """Each ``(alias, arguments)`` in *pages* as an ``items`` of its own.

    Ordered by name, so the groups of Alice, Bob and Charlie come first.
    """
    selections = " ".join(
        f"{alias}: items({arguments})" + " { edges { node { name } } }"
        for alias, arguments in pages
    )
    return (
        "{ users(first: 1, order: [{field: {name: ASC}}], "
        "groupBy: [{objects: {posts: {field: {title: true}}}}]) "
        "{ groups { aggregates { count } " + selections + " } } }"
    )


def _items_reads(statements: list[str]) -> list[str]:
    """The statements that read a page of every group's items, by row number."""
    return [s for s in statements if "ROW_NUMBER" in s.upper()]


def _forward_schema(orm, User, connection, *, max_results=None):
    """A root connection that names its node type before the type exists.

    The name is resolved in this module, so the caller puts the type there.
    """

    @strawberry.type
    class Query:
        @orm.connection(connection["ForwardUserNode"], max_results=max_results)
        def users(self) -> list["ForwardUserNode"]:  # noqa: F821
            return orm.get_default_queryset(User)

    @orm.type(User)
    class ForwardUserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    return Query, ForwardUserNode


def _key_subqueries(statement: str) -> list[str]:
    """The rows each ``objects:`` key in *statement* is built from, as SQL.

    A key is ``json_group_array(...)`` over ``FROM (<rows>)``, on SQLite.
    """
    found = []
    lowered = statement.lower()
    start = lowered.find("json_group_array(")
    while start != -1:
        opening = lowered.index("from (", start) + len("from ")
        depth = 0
        for end in range(opening, len(statement)):
            depth += {"(": 1, ")": -1}.get(statement[end], 0)
            if depth == 0:
                break
        found.append(statement[opening + 1 : end])
        start = lowered.find("json_group_array(", end)
    return found


def _items_by_count(result) -> dict[int, tuple[list[str], bool, bool]]:
    assert result.errors is None, result.errors
    found = {}
    for group in result.data["users"]["groups"]:
        items = group["items"]
        names = [edge["node"]["name"] for edge in items["edges"]]
        flags = items["pageInfo"]["hasPreviousPage"], items["pageInfo"]["hasNextPage"]
        found[group["aggregates"]["count"]] = (names, *flags)
    return found


class AbstractTestPaginationLimits:
    async def test_a_negative_after_cursor_is_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as seen:
            result = await run(schema, _page(f'first: 3, after: "{cursor(-5000)}"'))
        assert result.errors is not None, result.data
        assert result.errors[0].message == (
            "Argument 'after' cannot point before the first item."
        )
        assert seen.loaded == 0, seen.loaded

    async def test_a_negative_before_cursor_is_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as seen:
            result = await run(schema, _page(f'before: "{cursor(-3)}"'))
        assert result.errors is not None, result.data
        assert result.errors[0].message == (
            "Argument 'before' cannot point before the first item."
        )
        assert seen.loaded == 0, seen.loaded

    async def test_before_alone_reads_one_page(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post, max_results=5)
        with capture() as seen:
            result = await run(schema, _page(f'before: "{cursor(10**15)}"'))
        assert _names(result) == many_users[:5], _names(result)
        assert _flags(result) == (False, True), _flags(result)
        assert seen.loaded <= 6, seen.loaded

    async def test_no_page_arguments_read_one_page(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post, max_results=5)
        with capture() as seen:
            result = await run(schema, _page(""))
        assert _names(result) == many_users[:5], _names(result)
        assert _flags(result) == (False, True), _flags(result)
        assert seen.loaded <= 6, seen.loaded

    async def test_a_nested_connection_without_page_arguments_reads_one_page(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        """Each parent's page is cut in the database, as ``first`` would cut it."""
        schema = _nested_schema(orm_factory(), User, Post, max_results=1)
        with capture() as seen:
            result = await run(schema, _nested_posts(""))
        with capture() as asked:
            expected = await run(schema, _nested_posts("first: 1"))
        pages = _post_pages(result)
        assert pages == _post_pages(expected), pages
        assert pages["Alice"][1] is True, pages
        assert seen.statements == asked.statements, seen.statements

    async def test_last_zero_is_an_empty_page_at_the_end(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as seen:
            result = await run(schema, _page("last: 0"))
        assert _names(result) == [], _names(result)
        total = result.data["users"]["totalCount"]
        assert total == 12, total
        assert _flags(result) == (True, False), _flags(result)
        assert seen.loaded <= 1, seen.loaded

    async def test_last_zero_after_the_last_row_has_nothing_before_it(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(), User, Post)
        result = await run(schema, _page(f'last: 0, after: "{cursor(11)}"'))
        assert _names(result) == [], _names(result)
        assert _flags(result) == (False, False), _flags(result)

    async def test_more_groups_than_max_groups_are_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(max_groups=5), User, Post)
        with capture() as seen:
            result = await run(schema, _grouped("{field: {id: true}}"))
        assert result.errors is not None, result.data
        assert result.errors[0].message == (
            "groupBy makes more than 5 groups; filter the connection "
            "or group by fewer values"
        )
        grouped = [s for s in seen.statements if "GROUP BY" in s.upper()]
        assert grouped, seen.statements
        assert all("LIMIT" in s.upper() for s in grouped), grouped

    async def test_as_many_groups_as_max_groups_are_answered(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(max_groups=12), User, Post)
        result = await run(schema, _grouped("{field: {id: true}}"))
        assert result.errors is None, result.errors
        assert len(result.data["users"]["groups"]) == 12, result.data

    async def test_a_group_key_holding_too_many_related_rows_is_refused(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(max_group_key_rows=1), User, Post)
        result = await run(
            schema, _grouped("{objects: {posts: {field: {title: true}}}}")
        )
        assert result.errors is not None, result.data
        assert result.errors[0].message == (
            "objects: puts more than 1 related rows in one group key"
        )

    async def test_a_group_key_within_max_group_key_rows_is_answered(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(max_group_key_rows=2), User, Post)
        result = await run(
            schema, _grouped("{objects: {posts: {field: {title: true}}}}")
        )
        assert result.errors is None, result.errors
        assert result.data["users"]["groups"], result.data

    async def test_a_group_key_reads_no_more_related_rows_than_it_may_hold(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(max_group_key_rows=1), User, Post)
        with capture() as seen:
            result = await run(
                schema, _grouped("{objects: {posts: {field: {title: true}}}}")
            )
        assert result.errors is not None, result.data
        keys = [key for s in seen.statements for key in _key_subqueries(s)]
        assert keys, seen.statements
        for key in keys:
            assert "LIMIT" in key.upper(), key

    async def test_an_order_by_a_set_reads_every_related_row(
        self, orm_factory, User, Post, many_users, run
    ):
        query = (
            "{ users(first: 4, order: [{objects: {posts: {field: {title: DESC}}}}, "
            "{field: {name: ASC}}]) { edges { node { name } } } }"
        )
        expected = await run(_schema(orm_factory(), User, Post), query)
        capped = await run(
            _schema(orm_factory(max_group_key_rows=1), User, Post), query
        )
        assert capped.errors is None, capped.errors
        assert capped.data == expected.data, (capped.data, expected.data)

    async def test_a_groups_items_last_zero_is_an_empty_page_at_the_end(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as none_at_the_cursor:
            await run(schema, _group_items("first: 0"))
        with capture() as seen:
            result = await run(schema, _group_items("last: 0"))
        found = _items_by_count(result)
        assert found[9] == ([], True, False), found[9]
        assert all(names == [] for names, *_ in found.values()), found
        assert seen.loaded <= none_at_the_cursor.loaded, seen.loaded

    async def test_a_groups_items_last_zero_after_its_last_row_has_nothing_before_it(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(), User, Post)
        result = await run(schema, _group_items(f'last: 0, after: "{cursor(8)}"'))
        assert _items_by_count(result)[9] == ([], False, False)

    async def test_a_groups_items_page_is_unchanged(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(), User, Post)
        result = await run(schema, _group_items("first: 2"))
        assert _items_by_count(result)[9] == (many_users[3:5], False, True)
        result = await run(schema, _group_items("last: 2"))
        assert _items_by_count(result)[9] == (many_users[-2:], True, False)

    async def test_a_groups_items_are_held_to_the_connections_max_results(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post, max_results=5)
        connection = await run(schema, _page("first: 6"))
        with capture() as seen:
            result = await run(schema, _group_items("first: 6"))
        assert connection.errors[0].message == (
            "Argument 'first' cannot be higher than 5."
        )
        assert result.errors[0].message == connection.errors[0].message
        assert result.data == {"users": {"groups": None}}, result.data
        assert _items_reads(seen.statements) == [], seen.statements
        result = await run(schema, _group_items("last: 6"))
        assert result.errors[0].message == "Argument 'last' cannot be higher than 5."

    async def test_a_groups_items_without_page_arguments_read_one_page(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post, max_results=5)
        with capture() as asked:
            expected = await run(schema, _group_items("first: 5"))
        with capture() as seen:
            result = await run(schema, _group_items(""))
        assert _items_by_count(result)[9] == (many_users[3:8], False, True)
        assert result.data == expected.data, result.data
        assert seen.loaded == asked.loaded, seen.loaded

    async def test_a_groups_items_before_alone_reads_one_page(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post, max_results=5)
        with capture() as asked:
            expected = await run(schema, _group_items("first: 5"))
        with capture() as seen:
            result = await run(schema, _group_items(f'before: "{cursor(10**15)}"'))
        assert _items_by_count(result)[9] == (many_users[3:8], False, True)
        assert result.data == expected.data, result.data
        assert seen.loaded == asked.loaded, seen.loaded

    async def test_a_cursor_before_a_groups_first_item_is_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as seen:
            result = await run(
                schema, _group_items(f'first: 3, after: "{cursor(-5000)}"')
            )
        assert result.errors[0].message == (
            "Argument 'after' cannot point before the first item."
        )
        assert result.data == {"users": {"groups": None}}, result.data
        assert _items_reads(seen.statements) == [], seen.statements
        result = await run(schema, _group_items(f'before: "{cursor(-3)}"'))
        assert result.errors[0].message == (
            "Argument 'before' cannot point before the first item."
        )

    async def test_each_items_selected_on_a_group_gets_its_own_page(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(), User, Post)
        result = await run(
            schema,
            _aliased_group_items(
                ("a", "first: 1"), ("b", f'first: 2, after: "{cursor(6)}"')
            ),
        )
        assert result.errors is None, result.errors

        def names(*found):
            return {"edges": [{"node": {"name": name}} for name in found]}

        assert result.data == {
            "users": {
                "groups": [
                    {"aggregates": {"count": 1}, "a": names("Alice"), "b": names()},
                    {"aggregates": {"count": 1}, "a": names("Bob"), "b": names()},
                    {"aggregates": {"count": 1}, "a": names("Charlie"), "b": names()},
                    {
                        "aggregates": {"count": 9},
                        "a": names(many_users[3]),
                        "b": names(*many_users[10:12]),
                    },
                ]
            }
        }, result.data

    async def test_items_pages_far_apart_do_not_read_the_rows_between(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as near:
            await run(schema, _aliased_group_items(("a", "first: 1")))
        with capture() as seen:
            result = await run(
                schema,
                _aliased_group_items(
                    ("a", "first: 1"), ("b", f'first: 1, after: "{cursor(7)}"')
                ),
            )
        assert result.errors is None, result.errors
        nine = result.data["users"]["groups"][3]
        assert nine == {
            "aggregates": {"count": 9},
            "a": {"edges": [{"node": {"name": many_users[3]}}]},
            "b": {"edges": [{"node": {"name": many_users[11]}}]},
        }, nine
        # The far page reads the ninth of the nine, and none of the rows between.
        assert len(_items_reads(seen.statements)) == 2, seen.statements
        assert seen.loaded == near.loaded + 1, (seen.loaded, near.loaded)


class AbstractTestEveryConnectionCutsItsPage:
    """However a connection type is named, its page is cut in the database."""

    @pytest.mark.parametrize(
        "connection",
        [relay.Connection, relay.ListConnection, ORMConnection, ORMListConnection],
        ids=lambda connection: connection.__name__,
    )
    async def test_a_forward_referenced_node_type_is_held_to_one_page(
        self, orm_factory, User, many_users, capture, run, monkeypatch, connection
    ):
        orm = orm_factory()
        Query, node = _forward_schema(orm, User, connection, max_results=5)
        monkeypatch.setitem(globals(), "ForwardUserNode", node)
        schema = orm.schema(query=Query)
        with capture() as seen:
            result = await run(
                schema,
                "{ users { totalCount edges { node { name } } "
                "pageInfo { hasNextPage } } }",
            )
        assert result.errors is None, result.errors
        assert result.data == {
            "users": {
                "totalCount": 12,
                "edges": [{"node": {"name": name}} for name in many_users[:5]],
                "pageInfo": {"hasNextPage": True},
            }
        }, result.data
        assert seen.loaded <= 6, seen.loaded
        assert any("LIMIT" in s.upper() for s in seen.statements), seen.statements
        result = await run(
            schema,
            '{ users(after: "' + cursor(-5) + '") { edges { node { name } } } }',
        )
        assert result.errors[0].message == (
            "Argument 'after' cannot point before the first item."
        )

    def test_a_connection_type_that_slices_in_python_is_refused(
        self, orm_factory, User
    ):
        orm = orm_factory()

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        @strawberry.type
        class PlainUserConnection(relay.ListConnection[UserNode]):
            pass

        @strawberry.type
        class Query:
            @orm.connection(PlainUserConnection)
            def users(self) -> list[UserNode]:
                return orm.get_default_queryset(User)

        with pytest.raises(TypeError) as refused:
            orm.schema(query=Query)
        assert str(refused.value) == (
            "Query fields cannot be resolved. users: PlainUserConnection slices "
            "the rows it is given in Python, so its page would not be cut in the "
            "database or held to max_results and default_query_limit. Derive it "
            "from ORMListConnection."
        )


def _entries(entry: str, count: int) -> str:
    return "[" + ", ".join([entry] * count) + "]"


def _related_schema(orm, User, Post, Comment):
    """Users and posts point at each other, and a comment at its parent comment."""

    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        author: UserNode

    class CommentNode(relay.Node):
        id: relay.NodeID[int]
        body: auto
        post: PostNode

    # Set here, as types local to a function cannot be named in a string.
    UserNode.__annotations__["posts"] = list[PostNode]
    CommentNode.__annotations__["parent"] = CommentNode | None
    for model, node in ((User, UserNode), (Post, PostNode), (Comment, CommentNode)):
        orm.type(model, order=orm.order(model), group=orm.group(model))(node)

    @strawberry.type
    class Query:
        users: ORMListConnection[UserNode] = orm.connection.eager()
        posts: ORMListConnection[PostNode] = orm.connection.eager()
        comments: ORMListConnection[CommentNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _input_cycle(start: Any) -> list[str] | None:
    """Input types from *start* back to one already on the way, if there are any."""
    path: list[str] = []
    done: set[str] = set()

    def visit(input_type: Any) -> list[str] | None:
        if input_type.name in path:
            return [*path[path.index(input_type.name) :], input_type.name]
        if input_type.name in done:
            return None
        path.append(input_type.name)
        for field in input_type.fields.values():
            inner = get_named_type(field.type)
            if isinstance(inner, GraphQLInputObjectType):
                found = visit(inner)
                if found:
                    return found
        done.add(path.pop())
        return None

    return visit(start)


class AbstractTestOrderAndGroupByLimits:
    def test_order_and_group_by_nest_no_deeper_than_the_schema(
        self, orm_factory, User, Post, Comment
    ):
        """Unlike a filter's, their ``object:`` has no depth cap, and needs none.

        Each offers only relations to models whose order or group-by type was
        built before it, so a relation to its own model, or back to one that
        leads to it, is never offered and the nesting ends where the schema
        does.
        """
        schema = _related_schema(orm_factory(), User, Post, Comment)._schema
        starts = {
            get_named_type(argument.type)
            for graphql_type in schema.type_map.values()
            if isinstance(graphql_type, GraphQLObjectType)
            for field in graphql_type.fields.values()
            for name, argument in field.args.items()
            if name in ("order", "groupBy")
        }
        assert {start.name for start in starts} >= {"CommentOrder", "CommentGroupBy"}
        for start in starts:
            assert _input_cycle(start) is None, _input_cycle(start)

    async def test_an_order_longer_than_the_default_cap_is_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        order = _entries("{field: {name: ASC}}", 11)
        with capture() as seen:
            result = await run(schema, "{ userList(order: " + order + ") { name } }")
        assert result.errors is not None, result.data
        assert result.errors[0].message == "order has 11 entries; maximum is 10"
        assert seen.statements == [], seen.statements

    async def test_an_order_within_the_cap_is_answered(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(), User, Post)
        order = _entries("{field: {name: DESC}}", 10)
        result = await run(schema, _page(f"first: 2, order: {order}"))
        assert _names(result) == ["User 12", "User 11"], _names(result)

    async def test_max_order_entries_sets_the_cap(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(max_order_entries=2), User, Post)
        order = _entries("{field: {name: ASC}}", 3)
        result = await run(schema, _page(f"first: 2, order: {order}"))
        assert result.errors is not None, result.data
        assert result.errors[0].message == "order has 3 entries; maximum is 2"

    async def test_a_group_by_longer_than_the_default_cap_is_refused(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(), User, Post)
        with capture() as seen:
            result = await run(
                schema, _grouped(", ".join(["{field: {name: true}}"] * 11))
            )
        assert result.errors is not None, result.data
        assert result.errors[0].message == "groupBy has 11 entries; maximum is 10"
        assert not [s for s in seen.statements if "GROUP BY" in s.upper()]

    async def test_max_group_by_entries_sets_the_cap(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(max_group_by_entries=1), User, Post)
        result = await run(
            schema, _grouped("{field: {name: true}}, {field: {id: true}}")
        )
        assert result.errors is not None, result.data
        assert result.errors[0].message == "groupBy has 2 entries; maximum is 1"


class AbstractTestDefaultQueryLimit:
    """``default_query_limit`` caps a page and a root list, not the connection."""

    async def test_it_caps_each_page_not_the_rows_paged_through(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(default_query_limit=5), User, Post)
        result = await run(schema, _page(f'first: 3, after: "{cursor(5)}"'))
        assert _names(result) == many_users[6:9], _names(result)
        total = result.data["users"]["totalCount"]
        assert total == 12, total
        assert _flags(result) == (True, True), _flags(result)

    async def test_it_caps_a_page_asked_for_with_more(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(default_query_limit=5), User, Post)
        with capture() as seen:
            result = await run(schema, _page("first: 10"))
        assert _names(result) == many_users[:5], _names(result)
        assert _flags(result) == (False, True), _flags(result)
        assert seen.loaded <= 6, seen.loaded

    async def test_it_caps_a_groups_items(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        schema = _schema(orm_factory(default_query_limit=5), User, Post)
        with capture() as asked:
            expected = await run(schema, _group_items("first: 5"))
        assert _items_by_count(expected)[9] == (many_users[3:8], False, True)
        for arguments in ("first: 10", ""):
            with capture() as seen:
                result = await run(schema, _group_items(arguments))
            assert result.data == expected.data, (arguments, result.data)
            assert seen.loaded == asked.loaded, (arguments, seen.loaded)

    async def test_it_is_the_page_when_none_is_asked_for(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(default_query_limit=5), User, Post)
        result = await run(schema, _page(""))
        assert _names(result) == many_users[:5], _names(result)
        assert _flags(result) == (False, True), _flags(result)

    async def test_it_is_a_nested_page_when_none_is_asked_for(
        self, orm_factory, User, Post, many_users, capture, run
    ):
        """The limit also holds the parents, so two of them share the window."""
        schema = _nested_schema(orm_factory(default_query_limit=2), User, Post)
        with capture() as seen:
            result = await run(schema, _nested_posts(""))
        with capture() as asked:
            expected = await run(schema, _nested_posts("first: 2"))
        pages = _post_pages(result)
        assert pages == _post_pages(expected), pages
        assert list(pages) == ["Alice", "Bob"], pages
        assert seen.statements == asked.statements, seen.statements

    async def test_it_cuts_a_root_list_after_its_filter_and_order(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(default_query_limit=3), User, Post)
        result = await run(
            schema,
            """{ userList(filter: {field: {name: {startsWith: "User"}}},
                          order: [{field: {name: DESC}}]) { name } }""",
        )
        assert result.errors is None, result.errors
        assert [user["name"] for user in result.data["userList"]] == [
            "User 12",
            "User 11",
            "User 10",
        ]

    async def test_it_leaves_a_nested_relation_list_whole(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(default_query_limit=1), User, Post)
        result = await run(schema, "{ userList { name posts { title } } }")
        assert result.errors is None, result.errors
        assert result.data["userList"] == [
            {
                "name": "Alice",
                "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
            }
        ]
