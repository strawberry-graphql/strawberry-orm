"""A root connection cuts its page in the database.

It used to run its query unbounded - every matching row, with its eager loads -
and only then slice the page in Python, so a ``first: 50`` page over a large
table cost the whole table on every request and every scroll.

The page has to come out exactly as before. The reference below is Strawberry's
own ``ListConnection`` slicing a Python list of the same rows, which is what an
ORM connection did with every query until now, so each page shape is checked
against it rather than against hand-written expectations.

Each backend supplies:

``execute(schema, query)``      runs a query (awaitable on the async variants)
``capture()``                   context manager recording ``statements`` (SQL)
                                and ``loaded`` (``User`` rows built from them)
``many_users``                  seeds the table; returns names in ``id`` order
``users_query(User, info)``     every user, ordered by ``id``
``published_authors(User, Post, info)``
                                users with a published post: a join to posts,
                                made ``distinct``
``posts_query(Post, info)``     every post, ordered by ``id``
"""

import base64
from collections.abc import Iterable

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def page_query(args: str) -> str:
    call = f"users({args})" if args else "users"
    return (
        f"{{ {call} {{ totalCount edges {{ cursor node {{ name }} }} "
        "pageInfo { hasNextPage hasPreviousPage startCursor endCursor } } }"
    )


#: Every shape of request the Relay arguments allow, over a 12-row table.
PAGE_SHAPES = [
    "",
    "first: 3",
    f'first: 3, after: "{cursor(2)}"',
    f'first: 5, after: "{cursor(9)}"',
    "first: 12",
    "first: 11",
    "first: 0",
    f'first: 3, after: "{cursor(40)}"',
    "last: 3",
    "last: 12",
    "last: 20",
    f'last: 3, after: "{cursor(4)}"',
    f'last: 20, after: "{cursor(8)}"',
    f'last: 3, after: "{cursor(40)}"',
    "last: 0",
    f'before: "{cursor(5)}"',
    f'before: "{cursor(5)}", last: 2',
    f'before: "{cursor(5)}", first: 2',
    f'first: 4, last: 2, after: "{cursor(1)}"',
    f'after: "{cursor(5)}", before: "{cursor(3)}"',
    # Cursors are the client's to forge: positions no database can address,
    # and negative ones, which slice a Python list from its end.
    f'first: 3, after: "{cursor(2**64)}"',
    f'last: 2, after: "{cursor(2**64)}"',
    f'before: "{cursor(2**64)}"',
    f'first: 3, after: "{cursor(-5)}"',
    f'last: 2, after: "{cursor(-5)}"',
    f'before: "{cursor(-3)}"',
]

#: Requests Strawberry rejects; the rejection must not change either.
REJECTED_SHAPES = [
    "first: 101",
    "last: 101",
    "first: -1",
    f'after: "{base64.b64encode(b"other:1").decode()}"',
]


@strawberry.type
class _Row(relay.Node):
    id: relay.NodeID[int]
    name: str


@strawberry.type(name="RowConnection")
class _ReferenceConnection(relay.ListConnection[_Row]):
    total_count: int | None = None

    @classmethod
    def resolve_connection(cls, nodes, *, info, **kwargs):
        connection = super().resolve_connection(nodes, info=info, **kwargs)
        connection.total_count = len(nodes)
        return connection


@strawberry.type
class _ReferenceQuery:
    @relay.connection(_ReferenceConnection)
    def users(self, info: strawberry.Info) -> list[_Row]:
        names = info.context["names"]
        return [_Row(id=i, name=name) for i, name in enumerate(names, 1)]


_reference_schema = strawberry.Schema(query=_ReferenceQuery)


def reference(query: str, names: list[str]) -> tuple[dict | None, list[str]]:
    """What slicing the whole list in Python answers for *query*."""
    result = _reference_schema.execute_sync(query, context_value={"names": names})
    return result.data, [error.message for error in result.errors or []]


def outcome(result) -> tuple[dict | None, list[str]]:
    return result.data, [error.message for error in result.errors or []]


def _users_schema(orm, User, users_query, authors_query, Post):
    @orm.type(User, filters=orm.filter(User), order=orm.order(User))
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @strawberry.type
    class Query:
        @orm.connection(ORMListConnection[UserNode])
        def users(self, info: strawberry.Info) -> Iterable[UserNode]:
            return users_query(User, info)

        @orm.connection(ORMListConnection[UserNode])
        def published_authors(self, info: strawberry.Info) -> Iterable[UserNode]:
            return authors_query(User, Post, info)

    return orm.schema(query=Query)


def _payload_schema(orm_factory, User, users_query):
    from tests.abstract.payload import policy

    orm = orm_factory(payload=policy(), warn_missing_scope=False)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @strawberry.type
    class Query:
        @orm.payload.connection(ORMListConnection[UserNode])
        def users(self):
            return users_query(User, None)

    return orm.schema(query=Query)


PAYLOAD_PAGE = (
    "{ users { errors { message } data(first: 3) { edges { node { name } } } } }"
)


def _posts_schema(orm, Post, posts_query):
    @orm.type(
        Post,
        filters=orm.filter(Post),
        order=orm.order(Post),
        group=orm.group(Post),
    )
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        is_published: auto

    @strawberry.type
    class Query:
        @orm.connection(ORMListConnection[PostNode])
        def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
            return posts_query(Post, info)

    return orm.schema(query=Query)


GROUPED = """
{
  posts(first: 2, groupBy: [{ field: { authorId: true } }]) {
    totalCount
    edges { node { title } }
    aggregates { count }
    pageInfo { hasNextPage aggregates { count } }
    groups {
      key { authorId }
      aggregates { count }
      edgeIndices
      items(first: 1) { edges { node { title } } }
    }
  }
}
"""

LAST_PAGE = f'{{ users(first: 5, after: "{cursor(8)}") {{ totalCount edges {{ node {{ name }} }} }} }}'
ONE_PAGE = "{ users(first: 3) { edges { node { name } } } }"
COUNTED_PAGE = "{ users(first: 3) { totalCount edges { node { name } } } }"
COUNT_ONLY = "{ users(first: 3) { totalCount } }"
TAIL = "{ users(last: 2) { edges { cursor node { name } } } }"
DISTINCT_PAGE = """
{
  publishedAuthors(first: 1) {
    totalCount
    edges { node { name } }
    pageInfo { hasNextPage }
  }
  tail: publishedAuthors(last: 1) {
    totalCount
    edges { node { name } }
    pageInfo { hasPreviousPage }
  }
}
"""


def _counts(statements: list[str]) -> list[str]:
    return [sql for sql in statements if "COUNT(" in sql.upper()]


class _Checks:
    """Assertions shared by the sync and async variants."""

    def _assert_matches_reference(self, results, names):
        for shape, result in zip(PAGE_SHAPES + REJECTED_SHAPES, results, strict=True):
            assert outcome(result) == reference(page_query(shape), names), shape

    def _assert_payload_page(self, result, seen, names):
        assert result.errors is None, result.errors
        page = result.data["users"]
        assert page["errors"] is None
        assert [edge["node"]["name"] for edge in page["data"]["edges"]] == names[:3]
        self._assert_one_limited_read(seen, rows=4)

    def _assert_one_limited_read(self, seen, rows):
        assert len(seen.statements) == 1, seen.statements
        assert "LIMIT" in seen.statements[0].upper(), seen.statements[0]
        assert seen.loaded == rows, f"read {seen.loaded} rows, wanted {rows}"

    def _assert_counted(self, result, seen, total):
        assert result.errors is None, result.errors
        assert result.data["users"]["totalCount"] == total
        assert len(_counts(seen.statements)) == 1, seen.statements

    def _assert_total_read_off_the_last_page(self, result, seen, names):
        assert result.errors is None, result.errors
        page = result.data["users"]
        assert [edge["node"]["name"] for edge in page["edges"]] == names[9:]
        assert page["totalCount"] == len(names)
        assert _counts(seen.statements) == [], seen.statements

    def _assert_distinct(self, result):
        assert result.errors is None, result.errors
        page = result.data["publishedAuthors"]
        assert page["totalCount"] == 2
        assert [edge["node"]["name"] for edge in page["edges"]] == ["Alice"]
        assert page["pageInfo"]["hasNextPage"] is True
        # The tail counts first to find its window, then cuts from the same query.
        tail = result.data["tail"]
        assert tail["totalCount"] == 2
        assert [edge["node"]["name"] for edge in tail["edges"]] == ["Charlie"]
        assert tail["pageInfo"]["hasPreviousPage"] is True

    def _assert_grouped(self, result):
        assert result.errors is None, result.errors
        posts = result.data["posts"]
        assert posts["totalCount"] == 4
        assert [edge["node"]["title"] for edge in posts["edges"]] == [
            "Hello World",
            "GraphQL Guide",
        ]
        assert posts["aggregates"] == {"count": 4}
        assert posts["pageInfo"] == {"hasNextPage": True, "aggregates": {"count": 2}}
        groups = {
            str(group["key"]["authorId"]): (
                group["aggregates"]["count"],
                group["edgeIndices"],
                [edge["node"]["title"] for edge in group["items"]["edges"]],
            )
            for group in posts["groups"]
        }
        assert groups == {
            "1": (2, [0, 1], ["Hello World"]),
            "2": (1, [], ["Draft Post"]),
            "3": (1, [], ["Rust Adventures"]),
        }


#: Pages over a query already capped at five rows by ``default_query_limit``.
CAPPED_SHAPES = [
    "first: 3",
    f'first: 3, after: "{cursor(2)}"',
    f'first: 3, after: "{cursor(3)}"',
    "first: 10",
    "last: 2",
    f'last: 2, after: "{cursor(1)}"',
]


def _capped_schema(orm_factory, User, Post, published_authors):
    orm = orm_factory(default_query_limit=5)

    def capped(User, info):
        # Unordered: Django cannot order a queryset once it has been sliced.
        return orm.get_default_queryset(User)

    return _users_schema(orm, User, capped, published_authors, Post)


class AbstractTestRelayConnectionPagination(_Checks):
    def _schema(self, orm, User, Post, users_query, published_authors):
        return _users_schema(orm, User, users_query, published_authors, Post)

    def test_a_capped_query_pages_within_its_cap(
        self, orm_factory, User, Post, published_authors, many_users, execute
    ):
        schema = _capped_schema(orm_factory, User, Post, published_authors)
        for shape in CAPPED_SHAPES:
            query = page_query(shape)
            assert outcome(execute(schema, query)) == reference(
                query, many_users[:5]
            ), shape

    def test_every_page_shape_matches_slicing_the_whole_list(
        self, orm, User, Post, users_query, published_authors, many_users, execute
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        results = [
            execute(schema, page_query(shape))
            for shape in PAGE_SHAPES + REJECTED_SHAPES
        ]
        self._assert_matches_reference(results, many_users)

    def test_first_reads_the_page_and_one_more_row(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = execute(schema, ONE_PAGE)
        assert result.errors is None, result.errors
        self._assert_one_limited_read(seen, rows=4)

    def test_a_payload_connection_reads_only_its_page(
        self, orm_factory, User, users_query, many_users, execute, capture
    ):
        schema = _payload_schema(orm_factory, User, users_query)
        with capture() as seen:
            result = execute(schema, PAYLOAD_PAGE)
        self._assert_payload_page(result, seen, many_users)

    def test_after_skips_in_the_database(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        query = f'{{ users(first: 3, after: "{cursor(8)}") {{ edges {{ node {{ name }} }} }} }}'
        with capture() as seen:
            result = execute(schema, query)
        assert result.errors is None, result.errors
        assert [e["node"]["name"] for e in result.data["users"]["edges"]] == (
            many_users[9:12]
        )
        self._assert_one_limited_read(seen, rows=3)

    def test_no_count_is_sent_unless_total_count_is_selected(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            execute(schema, ONE_PAGE)
        assert _counts(seen.statements) == []

        with capture() as seen:
            result = execute(schema, COUNTED_PAGE)
        self._assert_counted(result, seen, total=len(many_users))
        assert seen.loaded == 4

    def test_a_short_page_needs_no_count(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        """A page that ends early ends the result, so it carries the total."""
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = execute(schema, LAST_PAGE)
        self._assert_total_read_off_the_last_page(result, seen, many_users)

    def test_total_count_alone_reads_no_rows(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = execute(schema, COUNT_ONLY)
        self._assert_counted(result, seen, total=len(many_users))
        assert seen.loaded == 0

    def test_last_reads_only_the_tail(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = execute(schema, TAIL)
        assert result.errors is None, result.errors
        assert result.data["users"]["edges"] == [
            {"cursor": cursor(10), "node": {"name": many_users[10]}},
            {"cursor": cursor(11), "node": {"name": many_users[11]}},
        ]
        assert seen.loaded == 2
        assert len(_counts(seen.statements)) == 1, seen.statements

    def test_a_distinct_query_counts_distinct_rows(
        self, orm, User, Post, users_query, published_authors, seed, execute
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        self._assert_distinct(execute(schema, DISTINCT_PAGE))

    def test_groups_and_aggregates_are_unchanged(
        self, orm, Post, posts_query, seed, execute
    ):
        schema = _posts_schema(orm, Post, posts_query)
        self._assert_grouped(execute(schema, GROUPED))


class AbstractTestRelayConnectionPaginationAsync(_Checks):
    def _schema(self, orm, User, Post, users_query, published_authors):
        return _users_schema(orm, User, users_query, published_authors, Post)

    async def test_a_capped_query_pages_within_its_cap(
        self, orm_factory, User, Post, published_authors, many_users, execute
    ):
        schema = _capped_schema(orm_factory, User, Post, published_authors)
        for shape in CAPPED_SHAPES:
            query = page_query(shape)
            assert outcome(await execute(schema, query)) == reference(
                query, many_users[:5]
            ), shape

    async def test_every_page_shape_matches_slicing_the_whole_list(
        self, orm, User, Post, users_query, published_authors, many_users, execute
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        results = [
            await execute(schema, page_query(shape))
            for shape in PAGE_SHAPES + REJECTED_SHAPES
        ]
        self._assert_matches_reference(results, many_users)

    async def test_first_reads_the_page_and_one_more_row(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = await execute(schema, ONE_PAGE)
        assert result.errors is None, result.errors
        self._assert_one_limited_read(seen, rows=4)

    async def test_a_payload_connection_reads_only_its_page(
        self, orm_factory, User, users_query, many_users, execute, capture
    ):
        schema = _payload_schema(orm_factory, User, users_query)
        with capture() as seen:
            result = await execute(schema, PAYLOAD_PAGE)
        self._assert_payload_page(result, seen, many_users)

    async def test_after_skips_in_the_database(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        query = f'{{ users(first: 3, after: "{cursor(8)}") {{ edges {{ node {{ name }} }} }} }}'
        with capture() as seen:
            result = await execute(schema, query)
        assert result.errors is None, result.errors
        assert [e["node"]["name"] for e in result.data["users"]["edges"]] == (
            many_users[9:12]
        )
        self._assert_one_limited_read(seen, rows=3)

    async def test_no_count_is_sent_unless_total_count_is_selected(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            await execute(schema, ONE_PAGE)
        assert _counts(seen.statements) == []

        with capture() as seen:
            result = await execute(schema, COUNTED_PAGE)
        self._assert_counted(result, seen, total=len(many_users))
        assert seen.loaded == 4

    async def test_a_short_page_needs_no_count(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        """A page that ends early ends the result, so it carries the total."""
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = await execute(schema, LAST_PAGE)
        self._assert_total_read_off_the_last_page(result, seen, many_users)

    async def test_total_count_alone_reads_no_rows(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = await execute(schema, COUNT_ONLY)
        self._assert_counted(result, seen, total=len(many_users))
        assert seen.loaded == 0

    async def test_last_reads_only_the_tail(
        self,
        orm,
        User,
        Post,
        users_query,
        published_authors,
        many_users,
        execute,
        capture,
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        with capture() as seen:
            result = await execute(schema, TAIL)
        assert result.errors is None, result.errors
        assert result.data["users"]["edges"] == [
            {"cursor": cursor(10), "node": {"name": many_users[10]}},
            {"cursor": cursor(11), "node": {"name": many_users[11]}},
        ]
        assert seen.loaded == 2
        assert len(_counts(seen.statements)) == 1, seen.statements

    async def test_a_distinct_query_counts_distinct_rows(
        self, orm, User, Post, users_query, published_authors, seed, execute
    ):
        schema = self._schema(orm, User, Post, users_query, published_authors)
        self._assert_distinct(await execute(schema, DISTINCT_PAGE))

    async def test_groups_and_aggregates_are_unchanged(
        self, orm, Post, posts_query, seed, execute
    ):
        schema = _posts_schema(orm, Post, posts_query)
        self._assert_grouped(await execute(schema, GROUPED))
