"""A page ordered through a to-many relation is cut in SQL (SQLAlchemy).

The join repeats a post once per tag, and the rows are read with
``.unique()``, which keeps each post where its first row is. The page is cut
the same way in the database: each post takes the place of its first row, and
the page counts posts rather than joined rows, so ``first: 1`` reads one post
however many tags the posts have.

The seed tags post 1 python, post 2 python and graphql, post 3 nothing and
post 4 rust. SQLite puts a missing tag first in ascending order.
"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import event
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.pagination_limits import cursor
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag

BY_TAG_ASC = ["Draft Post", "GraphQL Guide", "Hello World", "Rust Adventures"]
BY_TAG_DESC = ["Rust Adventures", "Hello World", "GraphQL Guide", "Draft Post"]


def _schema(orm):
    @orm.type(SATag, order=orm.order(SATag))
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(SAPost, order=orm.order(SAPost))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    return orm.schema(query=Query)


def _posts(arguments: str, direction: str = "ASC") -> str:
    order = "[{object: {tags: {field: {name: " + direction + "}}}}]"
    return (
        "{ posts(" + arguments + ", order: " + order + ") { totalCount "
        "edges { node { title } } pageInfo { hasPreviousPage hasNextPage } } }"
    )


@pytest.fixture
def capture(sa_session):
    @contextmanager
    def _capture():
        seen = SimpleNamespace(statements=[])

        def before(conn, cursor, statement, params, context, executemany):
            seen.statements.append(statement)

        sa_session.expunge_all()
        event.listen(sa_session.bind, "before_cursor_execute", before)
        try:
            yield seen
        finally:
            event.remove(sa_session.bind, "before_cursor_execute", before)

    return _capture


@pytest.fixture
def run(orm, sa_session):
    schema = _schema(orm)

    def _run(query):
        result = schema.execute_sync(query, context_value={"session": sa_session})
        assert result.errors is None, result.errors
        return result.data["posts"]

    return _run


def _titles(page) -> list[str]:
    return [edge["node"]["title"] for edge in page["edges"]]


@pytest.mark.parametrize(
    ("direction", "expected"), [("ASC", BY_TAG_ASC), ("DESC", BY_TAG_DESC)]
)
def test_paging_one_post_at_a_time_visits_each_post_once(
    run, seed, direction, expected
):
    titles = []
    for position in range(len(expected)):
        after = f', after: "{cursor(position - 1)}"' if position else ""
        page = run(_posts(f"first: 1{after}", direction))
        titles += _titles(page)
        assert page["pageInfo"]["hasNextPage"] is (position < len(expected) - 1)
    assert titles == expected


def test_the_page_is_cut_by_the_database(run, seed, capture):
    with capture() as seen:
        page = run(_posts("first: 1"))
    assert _titles(page) == BY_TAG_ASC[:1]
    pages = [s for s in seen.statements if "count(" not in s.lower()]
    assert len(pages) == 1, seen.statements
    assert "LIMIT" in pages[0].upper(), pages[0]


def test_the_count_counts_posts_not_joined_rows(run, seed):
    assert run(_posts("first: 2"))["totalCount"] == 4


def test_a_scoped_type_is_scoped_once(orm, sa_session, seed, capture):
    @orm.type(SATag, order=orm.order(SATag))
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(SAPost, order=orm.order(SAPost))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(SAPost.title != "Draft Post")

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    with capture() as seen:
        result = orm.schema(query=Query).execute_sync(
            _posts("first: 2"), context_value={"session": sa_session}
        )
    assert result.errors is None, result.errors
    assert _titles(result.data["posts"]) == BY_TAG_ASC[1:3]
    pages = [s for s in seen.statements if "count(" not in s.lower()]
    assert len(pages) == 1, seen.statements
    assert pages[0].count("!=") == 1, pages[0]


def test_the_last_posts_are_the_last_to_appear(run, seed, capture):
    with capture() as seen:
        page = run(_posts("last: 2"))
    assert _titles(page) == BY_TAG_ASC[2:]
    assert page["pageInfo"] == {"hasPreviousPage": True, "hasNextPage": False}
    pages = [s for s in seen.statements if "count(" not in s.lower()]
    assert len(pages) == 1, seen.statements
    assert "LIMIT" in pages[0].upper(), pages[0]
