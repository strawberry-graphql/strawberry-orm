"""A page ordered through a to-many relation is cut in SQL under loader options.

A scope written with ``with_loader_criteria`` leaves an option on the
statement. The page numbers each post by its first appearance in the join all
the same, carrying the criteria to the statement that reads the page, so a
client's order through a to-many relation cannot make a connection read every
row of the join. Any other option may lean on the join, so with one the page
is the statement itself, narrowed to the posts numbered onto the page.

The seed tags post 1 python, post 2 python and graphql, post 3 nothing and
post 4 rust. SQLite puts a missing tag first in ascending order.
"""

from collections.abc import Iterable

import pytest
import strawberry
from sqlalchemy import event, select
from sqlalchemy.orm import contains_eager, selectinload, with_loader_criteria
from strawberry import relay

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.pagination_limits import cursor
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag

VISIBLE_BY_TAG = ["GraphQL Guide", "Hello World", "Rust Adventures"]


def _hide(model, condition):
    def scope_rows(cls, stmt, info):
        return stmt.options(
            with_loader_criteria(model, condition, include_aliases=True)
        )

    return classmethod(scope_rows)


@pytest.fixture
def schema(orm):
    @orm.type(SATag, order=orm.order(SATag))
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(SAPost, order=orm.order(SAPost))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]

        scope_rows = _hide(SAPost, SAPost.title != "Draft Post")

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    return orm.schema(query=Query)


@pytest.fixture
def run(schema, sa_session):
    def _run(arguments):
        statements: list[str] = []

        def before(conn, cursor, statement, *args):
            statements.append(statement)

        sa_session.expunge_all()
        event.listen(sa_session.bind, "before_cursor_execute", before)
        try:
            result = schema.execute_sync(
                "{ posts(" + arguments + ", order: [{object: {tags: {field: "
                "{name: ASC}}}}]) { totalCount edges { node { title } } "
                "pageInfo { hasNextPage } } }",
                context_value={"session": sa_session},
            )
        finally:
            event.remove(sa_session.bind, "before_cursor_execute", before)
        assert result.errors is None, result.errors
        pages = [s for s in statements if "count(" not in s.lower()]
        return result.data["posts"], pages

    return _run


def _titles(page):
    return [edge["node"]["title"] for edge in page["edges"]]


def test_the_page_is_cut_by_the_database(run, seed):
    page, statements = run("first: 1")
    assert _titles(page) == VISIBLE_BY_TAG[:1]
    assert len(statements) == 1, statements
    assert "LIMIT" in statements[0].upper(), statements[0]


def test_paging_visits_each_readable_post_once(run, seed):
    titles = []
    for position in range(len(VISIBLE_BY_TAG)):
        after = f', after: "{cursor(position - 1)}"' if position else ""
        page, _statements = run(f"first: 1{after}")
        titles += _titles(page)
        assert page["totalCount"] == len(VISIBLE_BY_TAG)
    assert titles == VISIBLE_BY_TAG


def test_the_last_page_is_cut_by_the_database(run, seed):
    page, statements = run("last: 2")
    assert _titles(page) == VISIBLE_BY_TAG[1:]
    assert len(statements) == 1, statements
    assert "LIMIT" in statements[0].upper(), statements[0]


def test_criteria_on_the_joined_rows_still_order_the_page(orm, sa_session, seed):
    """Hiding a tag from the join moves the post it tagged, as a full read does."""

    @orm.type(SATag, order=orm.order(SATag))
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(SAPost, order=orm.order(SAPost))
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        tags: list[TagNode]

        scope_rows = _hide(SATag, SATag.name != "python")

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()

    def titles(first):
        result = orm.schema(query=Query).execute_sync(
            "{ posts(first: " + str(first) + ", order: [{object: {tags: {field: "
            "{name: DESC}}}}]) { edges { node { title } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        return _titles(result.data["posts"])

    # Without python, Hello World has no tag and sorts with the untagged post.
    expected = ["Rust Adventures", "GraphQL Guide", "Hello World", "Draft Post"]
    assert [titles(n) for n in range(1, 5)] == [expected[:n] for n in range(1, 5)]


def test_other_loader_options_read_only_the_page(orm, sa_session, seed):
    """An option that may lean on the join keeps it, for the page's posts only."""

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
        @orm.connection(ORMListConnection[PostNode])
        def posts(self) -> Iterable[PostNode]:
            return (
                select(SAPost)
                .outerjoin(SAPost.tags)
                .options(contains_eager(SAPost.tags))
                .order_by(SATag.name)
            )

    statements: list[str] = []
    event.listen(
        sa_session.bind,
        "before_cursor_execute",
        lambda conn, cursor, statement, *args: statements.append(statement),
    )
    sa_session.expunge_all()
    result = orm.schema(query=Query).execute_sync(
        "{ posts(first: 2) { edges { node { title } } } }",
        context_value={"session": sa_session},
    )
    assert result.errors is None, result.errors
    assert _titles(result.data["posts"]) == ["Draft Post", "GraphQL Guide"]
    assert len(statements) == 1, statements
    assert "LIMIT" in statements[0].upper(), statements[0]
    # contains_eager filled each post's tags from the join, every one of them.
    graphql_guide = sa_session.get(SAPost, 2)
    assert sorted(tag.name for tag in graphql_guide.tags) == ["graphql", "python"]
    assert len(statements) == 1, statements


def test_a_root_list_is_cut_under_other_loader_options(orm_factory, sa_session, seed):
    orm = orm_factory(default_query_limit=2)

    @orm.type(SATag, order=orm.order(SATag))
    class TagNode:
        id: auto
        name: auto

    @orm.type(SAPost, order=orm.order(SAPost))
    class PostType:
        title: auto
        tags: list[TagNode]

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.options(selectinload(SAPost.comments))

    @strawberry.type
    class Query:
        posts: list[PostType] = orm.field.eager()

    result = orm.schema(query=Query).execute_sync(
        "{ posts(order: [{object: {tags: {field: {name: DESC}}}}]) { title } }",
        context_value={"session": sa_session},
    )
    assert result.errors is None, result.errors
    assert result.data["posts"] == [
        {"title": "Rust Adventures"},
        {"title": "Hello World"},
    ]
