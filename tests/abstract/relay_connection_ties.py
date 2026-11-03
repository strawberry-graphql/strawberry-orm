"""Rows that tie on a connection's order land on exactly one page each.

Each page is its own query, cut with ``LIMIT`` and ``OFFSET``. Where the order
is not unique a database may break ties differently on every page - a bounded
top-N sort, a full sort and an index scan each do it their own way - so a row
can turn up on two pages and another on none. Every cut therefore ends its
order with the primary key.

SQLite tends to break ties the same way each time, so a scrolled result alone
passes by luck of the plan. Each case also reads the statements and checks
that their ORDER BY ends with the primary key, and names it once.

Backends supply:

``execute(schema, query)``    runs a query (awaitable on the async variants)
``statements()``              context manager collecting the SQL sent
``tied``                      40 users sharing one email, ``User 01``..``40``;
                              ``User 01`` has 40 posts sharing one body, and
                              ``User 02`` one more
``by_email(User, info)``      every user, ordered by email in the resolver
``unordered(User, info)``     every user, in no particular order
``by_default_order``          ``unordered``, with the model's default order
                              set to email; skips where models have none
``tied_authors(User, info)``  the users with a tied post, joined and distinct
``first_twenty(query)``       a ``scope_rows`` keeping ``User 01``..``20``
"""

import re
from collections.abc import Iterable

import strawberry
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto

NAMES = [f"User {i:02}" for i in range(1, 41)]
TITLES = [f"Post {i:02}" for i in range(1, 41)]

PAGE = (
    "{ users(%s) { totalCount edges { node { name } } "
    "pageInfo { hasNextPage hasPreviousPage startCursor endCursor } } }"
)
CHILD_PAGE = (
    "{ allUsers { name posts(%s) { edges { node { title } } "
    "pageInfo { hasNextPage endCursor } } } }"
)
BY_EMAIL = "order: [{ field: { email: ASC } }]"


def _schema(orm, User, Post, query, *, scoped=None):
    @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        body: auto

    @orm.type(User, filters=orm.filter(User), order=orm.order(User))
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        email: auto
        posts: ORMListConnection[PostNode] = orm.connection.eager()

        if scoped is not None:

            @classmethod
            def scope_rows(cls, query, info):
                return scoped(query)

    @strawberry.type
    class Query:
        all_users: list[UserNode] = orm.field.eager()

        @orm.connection(ORMListConnection[UserNode])
        def users(self, info: strawberry.Info) -> Iterable[UserNode]:
            return query(User, info)

    return orm.schema(query=Query)


def _args(*parts):
    return ", ".join(part for part in parts if part)


def _ok(result):
    assert result.errors is None, result.errors
    return result.data


def forward(order=""):
    """Scroll ``first: 7`` pages after each end cursor; the names read."""
    names, after = [], None
    while True:
        cursor = f'after: "{after}"' if after else ""
        page = _ok((yield PAGE % _args("first: 7", cursor, order)))["users"]
        names += [edge["node"]["name"] for edge in page["edges"]]
        if not page["pageInfo"]["hasNextPage"]:
            return names
        after = page["pageInfo"]["endCursor"]


def backward(order=""):
    """Scroll ``last: 7`` pages before each start cursor; the names read."""
    names, before = [], None
    while True:
        cursor = f'before: "{before}"' if before else ""
        page = _ok((yield PAGE % _args("last: 7", cursor, order)))["users"]
        names = [edge["node"]["name"] for edge in page["edges"]] + names
        if not page["pageInfo"]["hasPreviousPage"]:
            return names
        before = page["pageInfo"]["startCursor"]


def children_forward():
    """Scroll ``User 01``'s posts, tied on body, through the windowed read."""
    titles, after = [], None
    while True:
        cursor = f'after: "{after}"' if after else ""
        args = _args("first: 7", cursor, "order: [{ field: { body: ASC } }]")
        users = _ok((yield CHILD_PAGE % args))["allUsers"]
        page = next(u for u in users if u["name"] == "User 01")["posts"]
        titles += [edge["node"]["title"] for edge in page["edges"]]
        if not page["pageInfo"]["hasNextPage"]:
            return titles
        after = page["pageInfo"]["endCursor"]


def _order_columns(clause):
    """Column names of an ORDER BY clause, unqualified and without direction."""
    columns = []
    for term in clause.split(","):
        term = re.sub(r"\b(ASC|DESC|NULLS\s+(FIRST|LAST))\b", "", term, flags=re.I)
        columns.append(re.sub(r"[\"`\[\]]", "", term).strip().split(".")[-1])
    return columns


def page_order(sql):
    """The ORDER BY a page statement is cut by."""
    clause = re.split(r"\bORDER BY\b", sql, flags=re.I)[-1]
    return _order_columns(re.split(r"\b(LIMIT|OFFSET)\b", clause, flags=re.I)[0])


def window_order(sql):
    """The ORDER BY inside a ``ROW_NUMBER() OVER (...)`` window."""
    over = re.search(r"OVER\s*\((.*?)\)", sql, flags=re.I | re.S).group(1)
    return _order_columns(re.split(r"\bORDER BY\b", over, flags=re.I)[-1])


class _Checks:
    """Assertions shared by the sync and async variants."""

    def _assert_every_row_once(self, names, expected=NAMES):
        assert len(names) == len(expected), f"{len(names)} rows for {len(expected)}"
        assert sorted(names) == sorted(expected), "a row repeated or went missing"

    def _assert_pages_end_with_the_key(self, statements, *, order=page_order):
        pages = [
            sql
            for sql in statements
            if ("ROW_NUMBER" if order is window_order else "LIMIT") in sql.upper()
            and "COUNT(" not in sql.upper()
        ]
        assert pages, statements
        for sql in pages:
            columns = order(sql)
            assert columns[-1] == "id", f"the cut is not unique: {sql}"
            assert columns.count("id") == 1, f"the key is in the order twice: {sql}"


class AbstractTestRelayConnectionTies(_Checks):
    def _scroll(self, execute, schema, scroll):
        query = next(scroll)
        try:
            while True:
                query = scroll.send(execute(schema, query))
        except StopIteration as done:
            return done.value

    def test_a_client_sort_on_a_tied_column(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = self._scroll(execute, schema, forward(BY_EMAIL))
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    def test_the_resolvers_order(
        self, orm, User, Post, by_email, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, by_email)
        with statements() as sent:
            names = self._scroll(execute, schema, forward())
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    def test_the_models_default_order(
        self, orm, User, Post, by_default_order, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, by_default_order)
        with statements() as sent:
            names = self._scroll(execute, schema, forward())
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    def test_no_order_at_all(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = self._scroll(execute, schema, forward())
        assert names == NAMES, "with no order, the key alone orders the pages"
        self._assert_pages_end_with_the_key(sent)

    def test_scrolling_backwards(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = self._scroll(execute, schema, backward(BY_EMAIL))
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    def test_a_distinct_query_counts_as_before(
        self, orm, User, Post, tied_authors, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, tied_authors)
        with statements() as sent:
            data = _ok(execute(schema, PAGE % _args("first: 1", BY_EMAIL)))
        assert data["users"]["totalCount"] == 2
        assert data["users"]["pageInfo"]["hasNextPage"] is True
        self._assert_pages_end_with_the_key(sent)

    def test_an_order_on_the_key_is_not_given_it_twice(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        newest_first = "order: [{ field: { email: ASC } }, { field: { id: DESC } }]"
        with statements() as sent:
            names = self._scroll(execute, schema, forward(newest_first))
        assert names == NAMES[::-1]
        self._assert_pages_end_with_the_key(sent)

    def test_a_scoped_type_keeps_its_scope(
        self, orm, User, Post, unordered, tied, execute, statements, first_twenty
    ):
        """Reordering clones the query; Django could not scope a sliced one again."""
        schema = _schema(orm, User, Post, unordered, scoped=first_twenty)
        with statements() as sent:
            names = self._scroll(execute, schema, forward(BY_EMAIL))
        self._assert_every_row_once(names, NAMES[:20])
        self._assert_pages_end_with_the_key(sent)

    def test_tied_children_of_one_parent(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            titles = self._scroll(execute, schema, children_forward())
        self._assert_every_row_once(titles, TITLES)
        self._assert_pages_end_with_the_key(sent, order=window_order)


class AbstractTestRelayConnectionTiesAsync(_Checks):
    async def _scroll(self, execute, schema, scroll):
        query = next(scroll)
        try:
            while True:
                query = scroll.send(await execute(schema, query))
        except StopIteration as done:
            return done.value

    async def test_a_client_sort_on_a_tied_column(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = await self._scroll(execute, schema, forward(BY_EMAIL))
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    async def test_the_resolvers_order(
        self, orm, User, Post, by_email, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, by_email)
        with statements() as sent:
            names = await self._scroll(execute, schema, forward())
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    async def test_the_models_default_order(
        self, orm, User, Post, by_default_order, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, by_default_order)
        with statements() as sent:
            names = await self._scroll(execute, schema, forward())
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    async def test_no_order_at_all(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = await self._scroll(execute, schema, forward())
        assert names == NAMES, "with no order, the key alone orders the pages"
        self._assert_pages_end_with_the_key(sent)

    async def test_scrolling_backwards(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            names = await self._scroll(execute, schema, backward(BY_EMAIL))
        self._assert_every_row_once(names)
        self._assert_pages_end_with_the_key(sent)

    async def test_a_distinct_query_counts_as_before(
        self, orm, User, Post, tied_authors, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, tied_authors)
        with statements() as sent:
            data = _ok(await execute(schema, PAGE % _args("first: 1", BY_EMAIL)))
        assert data["users"]["totalCount"] == 2
        assert data["users"]["pageInfo"]["hasNextPage"] is True
        self._assert_pages_end_with_the_key(sent)

    async def test_an_order_on_the_key_is_not_given_it_twice(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        newest_first = "order: [{ field: { email: ASC } }, { field: { id: DESC } }]"
        with statements() as sent:
            names = await self._scroll(execute, schema, forward(newest_first))
        assert names == NAMES[::-1]
        self._assert_pages_end_with_the_key(sent)

    async def test_a_scoped_type_keeps_its_scope(
        self, orm, User, Post, unordered, tied, execute, statements, first_twenty
    ):
        """Reordering clones the query; Django could not scope a sliced one again."""
        schema = _schema(orm, User, Post, unordered, scoped=first_twenty)
        with statements() as sent:
            names = await self._scroll(execute, schema, forward(BY_EMAIL))
        self._assert_every_row_once(names, NAMES[:20])
        self._assert_pages_end_with_the_key(sent)

    async def test_tied_children_of_one_parent(
        self, orm, User, Post, unordered, tied, execute, statements
    ):
        schema = _schema(orm, User, Post, unordered)
        with statements() as sent:
            titles = await self._scroll(execute, schema, children_forward())
        self._assert_every_row_once(titles, TITLES)
        self._assert_pages_end_with_the_key(sent, order=window_order)
