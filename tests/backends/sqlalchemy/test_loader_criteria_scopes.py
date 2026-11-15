"""A ``scope_rows`` written with ``with_loader_criteria`` holds wherever it is read.

SQLAlchemy applies ``with_loader_criteria`` from the outermost statement only,
so a scope embedded in another statement loses it, and with it every row it
hides. Counts, aggregates, groups and their items, filters through relations,
orders by related rows and the check on rows a resolver returns all embed one.

Here posts keep only the published ones (Bob's "Draft Post" is hidden), users
hide Charlie, and tags hide "python", all through ``with_loader_criteria``.
"""

import pytest
import strawberry
from sqlalchemy import create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    with_loader_criteria,
)
from strawberry import relay

from strawberry_orm import StrawberryORM, auto
from strawberry_orm.relay import ORMListConnection
from tests.backends.sqlalchemy.models import Post, Tag, User

VISIBLE = ["Hello World", "GraphQL Guide", "Rust Adventures"]


def published(cls, stmt, info):
    return stmt.options(with_loader_criteria(Post, Post.is_published.is_(True)))


def build():
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )
    for model in (User, Tag):
        orm.filter(model)
        orm.group(model)
    orm.order(Tag)

    @orm.type(Tag)
    class TagNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.options(with_loader_criteria(Tag, Tag.name != "python"))

    @orm.type(Post, name="PostSummary")
    class PostSummary(relay.Node):
        id: relay.NodeID[int]
        title: auto
        word_count: auto

        scope_rows = classmethod(published)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: ORMListConnection[PostSummary] = orm.connection.eager()

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.options(with_loader_criteria(User, User.name != "Charlie"))

    @orm.aggregate_type(Post)
    class PostAggregates:
        word_count: auto

    @orm.type(
        Post,
        filters=orm.filter(Post),
        order=orm.order(Post, allow_scoped_ordering=["tags"]),
        group=orm.group(Post),
        aggregate=PostAggregates,
    )
    class PostNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        word_count: auto
        author: UserNode | None
        tags: list[TagNode]

        scope_rows = classmethod(published)

    @strawberry.type
    class Query:
        posts: ORMListConnection[PostNode] = orm.connection.eager()
        users: list[UserNode] = orm.field.eager()

        @strawberry.field
        def post_rows(self, info: strawberry.Info) -> list[PostNode]:
            session = info.context["session"]
            return list(session.scalars(select(Post).order_by(Post.id)))

    return orm.schema(query=Query)


@pytest.fixture
def execute(sa_session, seed):
    schema = build()

    def run(query):
        sa_session.expunge_all()
        result = schema.execute_sync(query, context_value={"session": sa_session})
        assert result.errors is None, result.errors
        return result.data

    return run


def titles(connection):
    return [edge["node"]["title"] for edge in connection["edges"]]


def test_a_count_leaves_out_hidden_rows(execute):
    data = execute("{ posts(first: 1) { totalCount edges { node { title } } } }")
    assert data["posts"]["totalCount"] == len(VISIBLE)


def test_aggregates_leave_out_hidden_rows(execute):
    data = execute("{ posts { aggregates { count min { wordCount } } } }")
    assert data["posts"]["aggregates"] == {"count": 3, "min": {"wordCount": 100}}


def test_a_relation_connection_counts_only_visible_rows(execute):
    for page in ("", "(first: 1)"):
        data = execute(f"{{ users {{ name posts{page} {{ totalCount }} }} }}")
        assert {
            user["name"]: user["posts"]["totalCount"] for user in data["users"]
        } == {
            "Alice": 2,
            "Bob": 0,
        }, page


def test_groups_through_a_relation_leave_out_hidden_rows(execute):
    data = execute(
        "{ posts(groupBy: [{object: {author: {field: {name: true}}}}]) { groups {"
        " key { author { name } } aggregates { count }"
        " items { edges { node { title } } } } } }"
    )
    groups = {
        (group["key"]["author"] or {}).get("name"): (
            group["aggregates"]["count"],
            titles(group["items"]),
        )
        for group in data["posts"]["groups"]
    }
    assert groups == {
        "Alice": (2, ["Hello World", "GraphQL Guide"]),
        None: (1, ["Rust Adventures"]),
    }


def test_groups_by_related_rows_leave_out_hidden_ones(execute):
    data = execute(
        "{ posts(groupBy: [{objects: {tags: {field: {name: true}}}}]) { groups {"
        " key { objects { tags { name } } } aggregates { count } } } }"
    )
    keys = sorted(
        (
            sorted(tag["name"] for tag in group["key"]["objects"]["tags"]),
            group["aggregates"]["count"],
        )
        for group in data["posts"]["groups"]
    )
    assert keys == [([], 1), (["graphql"], 1), (["rust"], 1)]


def test_an_order_by_related_rows_leaves_out_hidden_ones(execute):
    data = execute(
        "{ posts(order: [{objects: {tags: {field: {name: ASC}}}}, {field: {id: ASC}}])"
        " { edges { node { title } } } }"
    )
    assert titles(data["posts"]) == ["GraphQL Guide", "Rust Adventures", "Hello World"]


@pytest.mark.parametrize(
    ("relation", "hidden", "visible"),
    [
        ("author", "Charlie", "Alice"),
        ("tags", "python", "rust"),
    ],
)
def test_a_filter_through_a_relation_does_not_reach_hidden_rows(
    execute, relation, hidden, visible
):
    def matching(name):
        return titles(
            execute(
                f"{{ posts(filter: {{object: {{{relation}: {{field: {{name: "
                f'{{exact: "{name}"}}}}}}}}}}) {{ edges {{ node {{ title }} }} }} }}'
            )["posts"]
        )

    assert matching(hidden) == []
    assert matching(visible)


def test_rows_a_resolver_returns_are_checked_with_the_whole_scope(execute):
    data = execute("{ postRows { title } }")
    assert [post["title"] for post in data["postRows"]] == VISIBLE


def test_rows_are_checked_whatever_their_key_attribute_is_called():
    class Base(DeclarativeBase):
        pass

    class Note(Base):
        __tablename__ = "notes"

        ident: Mapped[int] = mapped_column("id", primary_key=True)
        text: Mapped[str]
        hidden: Mapped[bool]

    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)

    @orm.type(Note)
    class NoteType:
        text: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.options(with_loader_criteria(Note, Note.hidden.is_(False)))

    @strawberry.type
    class Query:
        @strawberry.field
        def notes(self, info: strawberry.Info) -> list[NoteType]:
            return list(info.context["session"].scalars(select(Note)))

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Note(ident=1, text="shown", hidden=False),
                Note(ident=2, text="hidden", hidden=True),
            ]
        )
        session.commit()
        result = orm.schema(query=Query).execute_sync(
            "{ notes { text } }", context_value={"session": session}
        )
    assert result.errors is None, result.errors
    assert result.data == {"notes": [{"text": "shown"}]}
