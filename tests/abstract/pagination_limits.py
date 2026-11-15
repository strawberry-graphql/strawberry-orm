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

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def _schema(orm, User, Post, *, max_results=None):
    @orm.type(Post, group=orm.group(Post))
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
        "{ groups { aggregates { count } items(" + arguments + ") "
        "{ edges { node { name } } pageInfo { hasPreviousPage hasNextPage } } } } }"
    )


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


def _entries(entry: str, count: int) -> str:
    return "[" + ", ".join([entry] * count) + "]"


class AbstractTestOrderAndGroupByLimits:
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

    async def test_it_is_the_page_when_none_is_asked_for(
        self, orm_factory, User, Post, many_users, run
    ):
        schema = _schema(orm_factory(default_query_limit=5), User, Post)
        result = await run(schema, _page(""))
        assert _names(result) == many_users[:5], _names(result)
        assert _flags(result) == (False, True), _flags(result)

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
