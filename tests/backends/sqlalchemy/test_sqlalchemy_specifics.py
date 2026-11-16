"""What the SQLAlchemy backend alone has to get right.

Sessions, column types and the statements SQLAlchemy builds have no Django or
Tortoise counterpart, so these are not shared tests.
"""

import asyncio
import datetime
import sys
import threading
from collections.abc import AsyncGenerator

import pytest
import strawberry
from sqlalchemy import (
    BIGINT,
    SMALLINT,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Time,
    create_engine,
    event,
    select,
    text,
    union,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
    with_loader_criteria,
)
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm._async import in_async_context
from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _column_value,
    _liftable_criteria,
    _unordered,
)
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.payload import policy as payload_policy
from tests.backends.sqlalchemy.models import Base, Comment, Post, Tag, User


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


class _Statements:
    """The statements a session's engine runs while counting."""

    def __init__(self, session):
        self.engine = session.bind
        self.sql: list[str] = []

    def _record(self, conn, cursor, statement, params, context, executemany):
        self.sql.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)


class TestDateAndTimeLookups:
    """A date or time given as ISO text is compared as one, not as text."""

    @pytest.mark.parametrize(
        ("column_type", "text", "value"),
        [
            (Date(), "2026-09-08", datetime.date(2026, 9, 8)),
            (Date(), "2026-09-08T10:00:00Z", datetime.date(2026, 9, 8)),
            (DateTime(), "2026-09-08T10:00:00", datetime.datetime(2026, 9, 8, 10)),
            (
                DateTime(timezone=True),
                "2026-09-08T10:00:00Z",
                datetime.datetime(2026, 9, 8, 10, tzinfo=datetime.UTC),
            ),
            (Time(), "10:30:00", datetime.time(10, 30)),
            (Integer(), "7", "7"),
        ],
    )
    def test_text_is_read_as_the_column_holds_it(self, column_type, text, value):
        assert _column_value(Column("value", column_type), text) == value

    def test_a_malformed_date_is_refused_in_words(self):
        with pytest.raises(ValueError, match="'soon' is not an ISO 8601 datetime"):
            _column_value(Column("value", DateTime()), "soon")

    def test_a_filter_binds_the_column_type(self, sa_session, seed):
        orm = _orm()

        @orm.type(Post, filters=orm.filter(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            created_at: auto

        @strawberry.type
        class Query:
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        sa_session.execute(
            Post.__table__.update().values(created_at=datetime.datetime(2025, 1, 1))
        )
        sa_session.execute(
            Post.__table__.update()
            .where(Post.id.in_([1, 2]))
            .values(created_at=datetime.datetime(2026, 9, 8, 10))
        )
        result = orm.schema(query=Query).execute_sync(
            """{ posts(filter: {field: {createdAt: {
                range: {start: "2026-09-08T00:00:00", end: "2026-09-09"}}}}) {
                edges { node { title } } } }""",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        titles = [edge["node"]["title"] for edge in result.data["posts"]["edges"]]
        assert sorted(titles) == ["GraphQL Guide", "Hello World"]


class TestWithoutGreenlet:
    """A sync session needs nothing ``sqlalchemy.ext.asyncio`` brings."""

    def test_a_sync_session_is_not_async(self, sa_session, monkeypatch):
        # A None entry makes the module unimportable, as greenlet's absence does.
        monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", None)
        backend = SQLAlchemyBackend(dialect="sqlite")
        assert backend._is_async_session(sa_session) is False

    def test_a_connection_reads(self, sa_session, seed, monkeypatch):
        orm = _orm()

        @orm.type(Post, order=orm.order(Post))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @strawberry.type
        class Query:
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        schema = orm.schema(query=Query)
        monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", None)
        result = schema.execute_sync(
            "{ posts(order: [{field: {id: ASC}}]) { totalCount edges { node { title } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert result.data["posts"]["totalCount"] == 4

    def test_rows_handed_back_load_their_relations(self, sa_session, seed, monkeypatch):
        orm = _orm()

        @orm.type(User)
        class UserNode:
            id: auto
            name: auto

        @orm.type(Post)
        class PostNode:
            id: auto
            author: UserNode

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostNode]:
                return list(sa_session.scalars(select(Post).order_by(Post.id)))

        schema = orm.schema(query=Query)
        monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", None)
        result = schema.execute_sync(
            "{ posts { author { name } } }", context_value={"session": sa_session}
        )
        assert result.errors is None, result.errors
        assert result.data["posts"][2] == {"author": {"name": "Bob"}}


class TestASyncSessionUnderAsyncExecution:
    """``offload_sync_session`` keeps the backend's statements off the loop."""

    @pytest.fixture
    def sa_session(self, tmp_path):
        # A file database: in-memory SQLite gives each thread its own.
        engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()

    @pytest.fixture
    def threads(self, sa_session):
        """The thread of every statement; one on the loop's thread is refused."""
        seen: list[int] = []

        def refuse(conn, cursor, statement, params, context, executemany):
            seen.append(threading.get_ident())
            if in_async_context():
                raise RuntimeError("cannot use the database from the event loop")

        event.listen(sa_session.bind, "before_cursor_execute", refuse)
        yield seen
        event.remove(sa_session.bind, "before_cursor_execute", refuse)

    def _schema(self, rows=None, *, offload=True):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            offload_sync_session=offload,
        )

        @orm.type(Post)
        class PostNode:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(Post.is_published.is_(True))

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostNode]
            newest: list[PostNode] = orm.field.eager(
                on="posts", scope=lambda query, info: query.order_by(Post.id.desc())
            )

        @strawberry.type
        class Query:
            users_page: ORMListConnection[UserNode] = orm.connection.eager()

            @strawberry.field
            def users(self, info: strawberry.Info) -> list[UserNode]:
                return select(User).order_by(User.id)

            @strawberry.field
            def read_rows(self, info: strawberry.Info) -> list[UserNode]:
                return rows

        @strawberry.type
        class Subscription:
            @strawberry.subscription
            async def user(self, info: strawberry.Info) -> AsyncGenerator[UserNode]:
                yield select(User).where(User.id == 1)

        return orm.schema(query=Query, subscription=Subscription)

    _USERS = "users { name posts { title } newest { title } }"
    _EXPECTED = [
        {
            "name": "Alice",
            "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
            "newest": [{"title": "GraphQL Guide"}, {"title": "Hello World"}],
        },
        {"name": "Bob", "posts": [], "newest": []},
        {
            "name": "Charlie",
            "posts": [{"title": "Rust Adventures"}],
            "newest": [{"title": "Rust Adventures"}],
        },
    ]

    @pytest.mark.asyncio
    async def test_a_subscription_reads_on_a_worker_thread(
        self, sa_session, seed, threads
    ):
        events = await self._schema().subscribe(
            "subscription { user { name posts { title } newest { title } } }",
            context_value={"session": sa_session},
        )
        answers = [event async for event in events]
        assert [answer.errors for answer in answers] == [None]
        assert answers[0].data == {"user": self._EXPECTED[0]}
        assert threads
        assert threading.get_ident() not in threads

    @pytest.mark.asyncio
    async def test_a_query_answers_as_under_sync_execution(
        self, sa_session, seed, threads
    ):
        schema = self._schema()
        query = (
            "{ " + self._USERS + " usersPage { totalCount edges { node { name } } } }"
        )
        result = await schema.execute(query, context_value={"session": sa_session})
        assert result.errors is None, result.errors
        assert result.data["users"] == self._EXPECTED
        assert result.data["usersPage"]["totalCount"] == 3
        assert set(threads) == {threads[0]} != {threading.get_ident()}
        # The users, their posts, every user's newest posts at once, and the
        # page, short enough to be its own count.
        assert len(threads) == 4
        threads.clear()
        # Relations the first execution loaded are not trusted by the second.
        sa_session.expunge_all()
        synced = await asyncio.to_thread(
            schema.execute_sync, query, context_value={"session": sa_session}
        )
        assert synced.data == result.data
        assert len(threads) == 4

    @pytest.mark.asyncio
    async def test_rows_a_resolver_read_are_checked_on_a_worker_thread(
        self, sa_session, seed, threads
    ):
        rows = await asyncio.to_thread(
            lambda: list(sa_session.scalars(select(User).order_by(User.id)))
        )
        threads.clear()
        result = await self._schema(rows).execute(
            "{ readRows { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert result.data["readRows"] == [
            {key: user[key] for key in ("name", "posts")} for user in self._EXPECTED
        ]
        assert threads
        assert threading.get_ident() not in threads

    @pytest.mark.asyncio
    async def test_without_the_option_the_loop_reads(self, sa_session, seed, threads):
        result = await self._schema(offload=False).execute(
            "{ users { name } }", context_value={"session": sa_session}
        )
        assert [error.message for error in result.errors] == [
            "cannot use the database from the event loop"
        ]


class TestBatchedViews:
    """An ``on=`` view is one batched read, under a list or a connection."""

    def _schema(self):
        orm = _orm()

        @orm.type(Tag)
        class TagNode:
            id: auto
            name: auto

        @orm.type(Post)
        class PostNode:
            id: auto
            title: auto
            tags: list[TagNode]

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(Post.title != "Hello World")

        @orm.type(User, filters=orm.filter(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            newest: list[PostNode] = orm.field.eager(
                on="posts", scope=lambda query, info: query.order_by(Post.id.desc())
            )

            @orm.field.lazy
            def written(self, info: strawberry.Info) -> list[PostNode]:
                return select(Post).where(Post.author_id == self.id).order_by(Post.id)

        @strawberry.type
        class Query:
            users_page: ORMListConnection[UserNode] = orm.connection.eager()

            @strawberry.field
            def users(self, info: strawberry.Info) -> list[UserNode]:
                return select(User).order_by(User.id)

        return orm.schema(query=Query)

    def test_a_list_of_parents_reads_each_level_once(self, sa_session, seed):
        with _Statements(sa_session) as statements:
            result = self._schema().execute_sync(
                "{ users { name newest { title tags { name } } } }",
                context_value={"session": sa_session},
            )
        assert result.errors is None, result.errors
        assert result.data["users"] == [
            {
                "name": "Alice",
                "newest": [
                    {
                        "title": "GraphQL Guide",
                        "tags": [{"name": "python"}, {"name": "graphql"}],
                    }
                ],
            },
            {"name": "Bob", "newest": [{"title": "Draft Post", "tags": []}]},
            {
                "name": "Charlie",
                "newest": [{"title": "Rust Adventures", "tags": [{"name": "rust"}]}],
            },
        ]
        # The users, every user's view, and the views' tags: the batched rows
        # are neither scope-checked nor loaded again, a user at a time.
        assert len(statements.sql) == 3, statements.sql

    @pytest.mark.parametrize("more", [0, 9])
    def test_a_batched_resolver_under_a_connection_is_read_once(
        self, sa_session, seed, more
    ):
        sa_session.add_all(
            User(id=10 + n, name=f"U{n}", email=f"{n}@x") for n in range(more)
        )
        sa_session.add_all(
            Post(id=10 + n, title=f"P{n}", body="", author_id=10 + n)
            for n in range(more)
        )
        sa_session.flush()
        with _Statements(sa_session) as statements:
            result = self._schema().execute_sync(
                "{ usersPage { edges { node { name written { title tags { name } } } } } }",
                context_value={"session": sa_session},
            )
        assert result.errors is None, result.errors
        written = [
            [post["title"] for post in edge["node"]["written"]]
            for edge in result.data["usersPage"]["edges"]
        ]
        assert written == [
            ["GraphQL Guide"],
            ["Draft Post"],
            ["Rust Adventures"],
            *([f"P{n}"] for n in range(more)),
        ]
        # The page, every node's posts, and their tags: the same for 12 nodes.
        assert len(statements.sql) == 3, statements.sql

    @pytest.mark.parametrize(
        ("arguments", "names"),
        [("first: 1", ["Alice"]), ('filter: {field: {name: {exact: "Nobody"}}}', [])],
    )
    def test_a_connection_of_one_node_or_none(self, sa_session, seed, arguments, names):
        result = self._schema().execute_sync(
            "{ usersPage("
            + arguments
            + ") { edges { node { name newest { title } } } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        edges = result.data["usersPage"]["edges"]
        assert [edge["node"]["name"] for edge in edges] == names

    def test_edges_of_plain_objects_are_left_alone(self, sa_session):
        @strawberry.type
        class Thing(relay.Node):
            id: relay.NodeID[int]
            label: str

            @strawberry.field
            def shout(self) -> list[str]:
                return [self.label.upper()]

        @strawberry.type
        class Query:
            @relay.connection(relay.ListConnection[Thing])
            def things(self) -> list[Thing]:
                return [Thing(id=1, label="one"), Thing(id=2, label="two")]

        result = (
            _orm()
            .schema(query=Query)
            .execute_sync(
                "{ things { edges { node { shout } } } }",
                context_value={"session": sa_session},
            )
        )
        assert result.errors is None, result.errors
        assert [edge["node"]["shout"] for edge in result.data["things"]["edges"]] == [
            ["ONE"],
            ["TWO"],
        ]


class TestRowsAResolverHandsBack:
    """Relations load onto the caller's rows, in their session, once."""

    @pytest.fixture
    def make(self, sa_session, seed):
        sa_session.commit()
        # Rows the app reads itself are in a session of their own, never
        # flushed, and the getter returns a new session on every call.
        return sessionmaker(bind=sa_session.bind, autoflush=False)

    def _schema(self, make, read):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            session_getter=lambda info: make(),
        )

        @orm.type(User)
        class UserNode:
            id: auto
            name: auto

        @orm.type(Comment)
        class CommentNode:
            id: auto
            body: auto
            author: UserNode

        @orm.type(Post)
        class PostNode:
            id: auto
            title: auto
            author: UserNode
            comments: list[CommentNode]

        @strawberry.type
        class Share:
            post: PostNode

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostNode]:
                return read()

            @strawberry.field
            def shares(self, info: strawberry.Info) -> list[Share]:
                return [Share(post=row) for row in read()]

        return orm.schema(query=Query)

    def _run(self, sa_session, schema, query):
        with _Statements(sa_session) as statements:
            result = schema.execute_sync(query)
        assert result.errors is None, result.errors
        return result.data, statements.sql

    def test_rows_from_another_session_fill_in_place(self, sa_session, make):
        held: list[Post] = []

        def read():
            held[:] = make().scalars(select(Post).order_by(Post.id)).all()
            return held

        data, sql = self._run(
            sa_session,
            self._schema(make, read),
            "{ posts { title author { name } comments { body } } }",
        )
        assert data["posts"][0] == {
            "title": "Hello World",
            "author": {"name": "Alice"},
            "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
        }
        # The rows, a re-read of them joining their authors, their comments.
        assert len(sql) == 3, sql
        for row in held:
            state = sa_inspect(row)
            assert not {"author", "comments"} & state.unloaded

    @pytest.mark.parametrize("field", ["posts", "shares"])
    def test_rows_holding_the_selection_cost_nothing(self, sa_session, make, field):
        session = make()
        session.add_all(
            Post(id=100 + n, title=f"Extra {n}", body="", author_id=1)
            for n in range(16)
        )
        session.commit()

        def read():
            return (
                make()
                .scalars(
                    select(Post)
                    .options(joinedload(Post.author), selectinload(Post.comments))
                    .order_by(Post.id)
                )
                .all()
            )

        selection = "title author { name } comments { body }"
        query = (
            "{ posts { " + selection + " } }"
            if field == "posts"
            else "{ shares { post { " + selection + " } } }"
        )
        data, sql = self._run(sa_session, self._schema(make, read), query)
        assert len(data[field]) == 20
        # Only the resolver's own read: the rows, then their comments.
        assert len(sql) == 2, sql

    def test_only_a_missing_level_is_read(self, sa_session, make):
        def read():
            return (
                make()
                .scalars(
                    select(Post).options(selectinload(Post.comments)).order_by(Post.id)
                )
                .all()
            )

        data, sql = self._run(
            sa_session,
            self._schema(make, read),
            "{ posts { comments { body author { name } } } }",
        )
        assert data["posts"][0]["comments"] == [
            {"body": "Nice post!", "author": {"name": "Bob"}},
            {"body": "Thanks!", "author": {"name": "Alice"}},
        ]
        # The resolver's read of the posts and their comments, then the
        # comments again, joining their authors.
        assert len(sql) == 3, sql
        assert sql[2].startswith("SELECT comment.id"), sql[2]

    def test_a_detached_row_reads_in_the_getters_session(self, sa_session, make):
        def read():
            session = make()
            rows = session.scalars(select(Post).order_by(Post.id)).all()
            session.close()
            return rows

        data, _sql = self._run(
            sa_session,
            self._schema(make, read),
            "{ posts { title author { name } } }",
        )
        assert data["posts"][3] == {
            "title": "Rust Adventures",
            "author": {"name": "Charlie"},
        }

    def test_values_changed_in_memory_survive(self, sa_session, make):
        def read():
            rows = make().scalars(select(Post).order_by(Post.id)).all()
            rows[0].title = "Changed"
            return rows

        data, _sql = self._run(
            sa_session,
            self._schema(make, read),
            "{ posts { title author { name } } }",
        )
        assert data["posts"][0] == {"title": "Changed", "author": {"name": "Alice"}}


class TestAnOrderedScopeKeepsItsOrder:
    """An eager relation whose scope orders comes back in that order."""

    def _schema(self, scope):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
        )

        @orm.type(Comment)
        class CommentNode:
            id: auto
            body: auto

        @orm.type(Post)
        class PostNode:
            id: auto
            title: auto
            comments: list[CommentNode]

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: list[PostNode] = orm.field.eager(scope=scope)

        @strawberry.type
        class Query:
            users_page: ORMListConnection[UserNode] = orm.connection.eager()

            @strawberry.field
            def users(self, info: strawberry.Info) -> list[UserNode]:
                return select(User).order_by(User.id)

            @strawberry.field
            def user(self, info: strawberry.Info, id: int) -> UserNode | None:
                return select(User).where(User.id == id)

        return orm.schema(query=Query)

    def _run(self, sa_session, schema, query):
        with _Statements(sa_session) as statements:
            result = schema.execute_sync(query, context_value={"session": sa_session})
        assert result.errors is None, result.errors
        return result.data, statements.sql

    @staticmethod
    def _by_title(query, info):
        return query.order_by(Post.title)

    def test_under_a_list(self, sa_session, seed):
        data, sql = self._run(
            sa_session, self._schema(self._by_title), "{ users { posts { title } } }"
        )
        assert [[p["title"] for p in user["posts"]] for user in data["users"]] == [
            ["GraphQL Guide", "Hello World"],
            ["Draft Post"],
            ["Rust Adventures"],
        ]
        assert len(sql) == 2, sql

    def test_under_a_connection(self, sa_session, seed):
        data, sql = self._run(
            sa_session,
            self._schema(self._by_title),
            "{ usersPage { edges { node { posts { title } } } } }",
        )
        first = data["usersPage"]["edges"][0]["node"]
        assert [p["title"] for p in first["posts"]] == ["GraphQL Guide", "Hello World"]
        assert len(sql) == 2, sql

    def test_under_a_single_parent(self, sa_session, seed):
        data, sql = self._run(
            sa_session,
            self._schema(self._by_title),
            "{ user(id: 1) { posts { title } } }",
        )
        assert [p["title"] for p in data["user"]["posts"]] == [
            "GraphQL Guide",
            "Hello World",
        ]
        assert len(sql) == 2, sql

    def test_a_filter_and_an_order_both_hold_with_a_nested_selection(
        self, sa_session, seed
    ):
        def published_by_title_descending(query, info):
            return query.where(Post.is_published.is_(True)).order_by(Post.title.desc())

        data, sql = self._run(
            sa_session,
            self._schema(published_by_title_descending),
            "{ users { posts { title comments { body } } } }",
        )
        assert data["users"] == [
            {
                "posts": [
                    {
                        "title": "Hello World",
                        "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                    },
                    {"title": "GraphQL Guide", "comments": [{"body": "Great guide"}]},
                ]
            },
            {"posts": []},
            {"posts": [{"title": "Rust Adventures", "comments": []}]},
        ]
        assert len(sql) == 3, sql


def test_an_ordered_relation_past_a_to_one_relation(sa_session, seed):
    sa_session.add(Comment(id=10, body="Again", post_id=4, author_id=1))
    sa_session.flush()
    orm = _orm()

    @orm.type(Comment)
    class CommentNode:
        id: auto
        body: auto

    @orm.type(User)
    class UserNode:
        id: auto
        name: auto
        comments: list[CommentNode] = orm.field.eager(
            scope=lambda query, info: query.order_by(Comment.body)
        )

    @orm.type(Post)
    class PostNode:
        id: auto
        title: auto
        author: UserNode

    @strawberry.type
    class Query:
        @strawberry.field
        def posts(self, info: strawberry.Info) -> list[PostNode]:
            return select(Post).order_by(Post.id)

    with _Statements(sa_session) as statements:
        result = orm.schema(query=Query).execute_sync(
            "{ posts { author { comments { body } } } }",
            context_value={"session": sa_session},
        )
    assert result.errors is None, result.errors
    assert result.data["posts"][0]["author"]["comments"] == [
        {"body": "Again"},
        {"body": "Thanks!"},
    ]
    # The posts with their authors joined, then every author's comments.
    assert len(statements.sql) == 2, statements.sql


class _Users:
    """An app's own query object, wrapping the statement it will run."""

    def __init__(self, stmt):
        self.stmt = stmt


class TestAnAppsOwnQueryObject:
    """``to_query=`` maps an app's query objects to statements, everywhere."""

    def _schema(self):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            payload=payload_policy(),
            to_query=lambda result, info: (
                result.stmt if isinstance(result, _Users) else result
            ),
        )

        @orm.type(User, filters=orm.filter(User), order=orm.order(User))
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        everyone = select(User).order_by(User.id)

        @strawberry.type
        class Query:
            @orm.payload.query
            def users(self) -> list[UserNode]:
                return _Users(everyone)

            @orm.payload.connection(ORMListConnection[UserNode])
            def users_page(self):
                return _Users(everyone)

            @orm.field.lazy
            async def first_user(self, info: strawberry.Info) -> UserNode | None:
                return _Users(everyone.limit(1))

            @orm.connection.lazy(ORMListConnection[UserNode])
            def plain_page(self, info: strawberry.Info) -> list[UserNode]:
                return _Users(everyone)

        return orm.schema(query=Query)

    async def _run(self, sa_session, query):
        result = await self._schema().execute(
            query, context_value={"session": sa_session}
        )
        assert result.errors is None, result.errors
        return result.data

    @pytest.mark.asyncio
    async def test_a_payload_list_and_a_field(self, sa_session, seed):
        data = await self._run(
            sa_session, "{ users { data { name } } firstUser { name } }"
        )
        assert [user["name"] for user in data["users"]["data"]] == [
            "Alice",
            "Bob",
            "Charlie",
        ]
        assert data["firstUser"] == {"name": "Alice"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["usersPage", "plainPage"])
    async def test_a_connection_filters_orders_and_pages(self, sa_session, seed, field):
        def page(arguments):
            selection = (
                "totalCount pageInfo { hasNextPage endCursor } edges { node { name } }"
            )
            inner = "(" + arguments + ") { " + selection + " }"
            if field == "usersPage":
                return "{ usersPage { data" + inner + " } }"
            return "{ plainPage" + inner + " }"

        def unwrap(data):
            return (
                data["usersPage"]["data"] if field == "usersPage" else data["plainPage"]
            )

        filtered = unwrap(
            await self._run(sa_session, page('filter: {field: {name: {exact: "Bob"}}}'))
        )
        assert filtered["totalCount"] == 1
        assert [edge["node"]["name"] for edge in filtered["edges"]] == ["Bob"]

        first = unwrap(
            await self._run(
                sa_session, page("order: [{field: {name: DESC}}], first: 2")
            )
        )
        assert first["totalCount"] == 3
        assert [edge["node"]["name"] for edge in first["edges"]] == ["Charlie", "Bob"]
        cursor = first["pageInfo"]["endCursor"]
        rest = unwrap(
            await self._run(
                sa_session,
                page(
                    'order: [{field: {name: DESC}}], first: 2, after: "' + cursor + '"'
                ),
            )
        )
        assert [edge["node"]["name"] for edge in rest["edges"]] == ["Alice"]
        assert rest["pageInfo"]["hasNextPage"] is False


def test_field_order_puts_a_models_fields_in_the_apps_order():
    def relations_first(model, names):
        return ["comments", "posts", "missing"]

    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, field_order=relations_first
    )
    names = [name for name, *_ in orm.backend._introspect_model(User)]
    assert names == ["comments", "posts", "id", "name", "email", "created_at"]
    default = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)
    assert [name for name, *_ in default.backend._introspect_model(User)] == [
        "id",
        "name",
        "email",
        "created_at",
        "posts",
        "comments",
    ]


class _IntegerBase(DeclarativeBase):
    pass


class Counts(_IntegerBase):
    __tablename__ = "counts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    small: Mapped[int] = mapped_column(SmallInteger)
    big: Mapped[int] = mapped_column(BigInteger)
    small_dialect: Mapped[int] = mapped_column(SMALLINT)
    big_dialect: Mapped[int] = mapped_column(BIGINT)
    plain: Mapped[int] = mapped_column(Integer)


def test_every_integer_column_is_an_int():
    orm = _orm()

    @orm.type(Counts, group=orm.group(Counts))
    class CountsNode(relay.Node):
        id: relay.NodeID[int]
        small: auto
        big: auto
        small_dialect: auto
        big_dialect: auto
        plain: auto

    @strawberry.type
    class Query:
        counts: ORMListConnection[CountsNode] = orm.connection.eager()

    types = orm.schema(query=Query)._schema.type_map
    names = ["small", "big", "smallDialect", "bigDialect", "plain"]
    node = types["CountsNode"].fields
    assert {name: str(node[name].type) for name in names} == dict.fromkeys(
        names, "Int!"
    )
    for aggregate in ("CountsSumAggregates", "CountsAvgAggregates"):
        assert set(names) <= set(types[aggregate].fields)


class _RenamedBase(DeclarativeBase):
    pass


class Person(_RenamedBase):
    """Columns the database names after something other than their attribute."""

    __tablename__ = "person"

    key: Mapped[int] = mapped_column("id", primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    _phone: Mapped[str] = mapped_column("phone", String(20))
    password_hash: Mapped[str] = mapped_column("pw", String(100), default="x")

    @hybrid_property
    def phone(self) -> str:
        return "***-" + self._phone[-4:]

    @phone.inplace.expression
    @classmethod
    def _phone_expression(cls):
        return cls._phone


class TestAColumnIsNamedByItsAttribute:
    """The inputs read a column through its attribute, so they are named by it.

    The database's name can be another attribute's: here ``phone`` is a hybrid
    masking the ``_phone`` column, and ``password_hash`` is stored as ``pw``.
    """

    def test_the_model_is_read_by_its_attributes(self):
        backend = _orm().backend
        assert [name for name, *_ in backend._introspect_model(Person)] == [
            "key",
            "name",
            "_phone",
            "password_hash",
        ]
        assert backend._get_pk_names(Person) == {"key"}

    def test_a_sensitive_attribute_stays_out_whatever_its_column_is_called(self):
        orm = _orm()
        written = orm.input(Person).__strawberry_definition__.fields
        filtered = orm.filter(Person)._field_type.__strawberry_definition__.fields
        assert {field.python_name for field in written} == {"name", "_phone"}
        assert {field.python_name for field in filtered} == {"key", "name", "_phone"}

    def test_a_hybrid_named_like_the_column_does_not_open_it(self):
        orm = _orm()

        @orm.type(Person, filters=orm.filter(Person), group=orm.group(Person))
        class PersonNode(relay.Node):
            key: relay.NodeID[int]
            name: auto
            phone: str

        @strawberry.type
        class Query:
            people: ORMListConnection[PersonNode] = orm.connection.eager()

        schema = orm.schema(query=Query)
        engine = create_engine("sqlite://")
        _RenamedBase.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            session.add(Person(key=1, name="Alice", _phone="+1-555-867-5309"))
            session.commit()
            context = {"session": session}
            shown = schema.execute_sync(
                "{ people { edges { node { phone } } } }", context_value=context
            )
            grouped = schema.execute_sync(
                "{ people(groupBy: [{field: {phone: true}}]) "
                "{ groups { key { phone } } } }",
                context_value=context,
            )
        assert shown.errors is None, shown.errors
        assert shown.data == {"people": {"edges": [{"node": {"phone": "***-5309"}}]}}
        assert grouped.data is None
        assert [error.message for error in grouped.errors] == [
            "Expected value of type 'PersonGroupByField' not to include unknown "
            "field 'phone', found: { phone: true }.",
            "Cannot query field 'phone' on type 'PersonGroupKey'.",
        ]
        type_map = schema._schema.type_map
        for name in ("PersonField", "PersonGroupByField", "PersonGroupKey"):
            assert set(type_map[name].fields) == {"key", "name"}, name


def test_an_order_with_no_clauses_keeps_the_statements(Post):
    @strawberry.input
    class NoField:
        id: object = strawberry.UNSET

    @strawberry.input
    class NoOrder:
        field: NoField | None = strawberry.UNSET

    ordered = select(Post).order_by(Post.title)
    kept = SQLAlchemyBackend(dialect="sqlite").apply_ordering(ordered, NoOrder(), Post)
    assert str(kept) == str(ordered)


def test_a_scope_check_reads_only_keys(sa_session, seed):
    """A ``scope_rows`` that loads tags is not run whole to learn which rows pass."""
    orm = _orm()

    @orm.type(Post)
    class PostNode:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.where(Post.is_published.is_(True)).options(
                selectinload(Post.tags)
            )

    @strawberry.type
    class Query:
        @orm.field.lazy
        def posts(self, info: strawberry.Info) -> list[PostNode]:
            return list(sa_session.scalars(select(Post).order_by(Post.id)))

    schema = orm.schema(query=Query)
    with _Statements(sa_session) as statements:
        result = schema.execute_sync(
            "{ posts { title } }", context_value={"session": sa_session}
        )
    assert result.errors is None, result.errors
    assert [post["title"] for post in result.data["posts"]] == [
        "Hello World",
        "GraphQL Guide",
        "Rust Adventures",
    ]
    checks = statements.sql[1:]
    assert len(checks) == 1, statements.sql
    assert checks[0].startswith("SELECT post.id \nFROM post")
    assert "post_tag" not in checks[0]


class _SharedTableBase(DeclarativeBase):
    pass


class _Owner(_SharedTableBase):
    __tablename__ = "lifting_owner"
    id: Mapped[int] = mapped_column(primary_key=True)


class _Item(_SharedTableBase):
    __tablename__ = "lifting_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("lifting_owner.id"))
    kind: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "item"}


class _SpecialItem(_Item):
    """Shares its parent's table; the ORM adds ``kind = 'special'`` itself."""

    __mapper_args__ = {"polymorphic_identity": "special"}


class TestScopeLifting:
    """A scope over a relation's own columns is applied as its WHERE clause.

    Embedded whole, it would be ``pk IN (SELECT ...)`` inside a correlated
    EXISTS, which SQLite runs again for every outer row.
    """

    def test_a_base_class_sharing_its_table_is_lifted(self):
        named = _Item.name == "x"
        lifted = _liftable_criteria(select(_Item).where(named), _Item, _Owner)
        assert str(lifted) == str(named)

    def test_a_scope_over_its_own_columns_is_its_where_clause(self):
        published = Post.is_published.is_(True)
        lifted = _liftable_criteria(select(Post).where(published), Post, User)
        assert str(lifted) == str(published)

    @pytest.mark.parametrize(
        ("scoped", "related", "parent"),
        [
            pytest.param(
                union(select(Post.id), select(Post.id)), Post, User, id="a union"
            ),
            pytest.param(
                select(Comment).where(Comment.body != ""),
                Comment,
                Comment,
                id="a relation back to the same table",
            ),
            pytest.param(select(Post), Post, User, id="no where clause"),
            pytest.param(
                select(Post).where(
                    Post.author_id.in_(select(User.id).where(User.name == "Alice"))
                ),
                Post,
                User,
                id="a nested select",
            ),
            pytest.param(
                select(Post).join(Post.author).where(User.name == "Alice"),
                Post,
                User,
                id="a join",
            ),
            pytest.param(
                select(Post).where(Post.is_published.is_(True)).limit(1),
                Post,
                User,
                id="a limit",
            ),
            pytest.param(
                select(Post)
                .where(Post.is_published.is_(True))
                .options(with_loader_criteria(Post, Post.title != "Draft Post")),
                Post,
                User,
                id="loader criteria",
            ),
            pytest.param(
                select(Post).where(text("is_published = 1")),
                Post,
                User,
                id="raw sql",
            ),
            pytest.param(
                select(_SpecialItem).where(_SpecialItem.name == "x"),
                _SpecialItem,
                _Owner,
                id="a class sharing its parent's table",
            ),
            pytest.param(
                select(_SpecialItem).where(_SpecialItem.name == "x"),
                _Item,
                _Owner,
                id="another entity than the relation's",
            ),
        ],
    )
    def test_any_other_scope_is_kept_whole(self, scoped, related, parent):
        assert _liftable_criteria(scoped, related, parent) is None

    def test_a_count_keeps_the_order_distinct_on_picks_rows_by(self):
        ordered = select(Post).order_by(Post.author_id, Post.id.desc())
        assert not _unordered(ordered)._order_by_clauses
        first_per_author = ordered.distinct(Post.author_id)
        assert _unordered(first_per_author)._order_by_clauses

    @pytest.mark.parametrize(
        ("scope", "kept_whole"),
        [
            pytest.param(
                lambda q: q.where(User.name == "Alice"), False, id="own columns"
            ),
            pytest.param(
                lambda q: q.join(User.posts).where(Post.is_published.is_(True)),
                True,
                id="a join",
            ),
        ],
    )
    def test_a_filter_through_a_scoped_relation(
        self, sa_session, seed, scope, kept_whole
    ):
        orm = _orm()

        @orm.type(User, filters=orm.filter(User))
        class UserNode:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, query, info):
                return scope(query)

        @orm.type(Post, filters=orm.filter(Post))
        class PostNode:
            id: auto
            title: auto
            author: UserNode

        @strawberry.type
        class Query:
            posts: list[PostNode] = orm.field.auto()

        schema = orm.schema(query=Query)
        with _Statements(sa_session) as statements:
            result = schema.execute_sync(
                '{ posts(filter: { object: { author: { field: { name: { exact: "Bob" } } } } })'
                " { title } }",
                context_value={"session": sa_session},
            )

        assert result.errors is None, result.errors
        assert result.data == {"posts": []}
        assert ("IN (SELECT" in statements.sql[0]) is kept_whole, statements.sql[0]


def test_a_relation_fields_order_replaces_its_scopes(sa_session, seed):
    orm = _orm()

    @orm.type(Post, order=orm.order(Post))
    class PostNode:
        id: auto
        title: auto

    @orm.type(User)
    class UserNode:
        id: auto
        posts: list[PostNode] = orm.field.eager(
            scope=lambda query, info: query.order_by(Post.title),
            order=orm.order(Post),
        )

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self, info: strawberry.Info) -> list[UserNode]:
            return select(User).where(User.id == 1)

    schema = orm.schema(query=Query)
    titles = {}
    for name, arguments in [("scope", ""), ("client", "(order: [{field: {id: ASC}}])")]:
        result = schema.execute_sync(
            "{ users { posts" + arguments + " { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        titles[name] = [post["title"] for post in result.data["users"][0]["posts"]]
    assert titles == {
        "scope": ["GraphQL Guide", "Hello World"],
        "client": ["Hello World", "GraphQL Guide"],
    }


def test_a_collection_the_app_holds_is_left_alone(sa_session, seed):
    """A loaded relation is not replaced by a scoped read: it may hold changes."""
    sa_session.commit()
    make = sessionmaker(bind=sa_session.bind, autoflush=False)
    alice = make().get(User, 1)
    pending = Post(title="Unsaved", body="", author_id=1)
    alice.posts.append(pending)
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        session_getter=lambda info: make(),
    )

    @orm.type(Post)
    class PostNode:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return query.where(Post.title != "Hello World")

    @orm.type(User)
    class UserNode:
        id: auto
        posts: list[PostNode]

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self, info: strawberry.Info) -> list[UserNode]:
            return [alice]

    result = orm.schema(query=Query).execute_sync("{ users { posts { title } } }")
    assert result.errors is None, result.errors
    assert "Hello World" not in [p["title"] for p in result.data["users"][0]["posts"]]
    assert pending in alice.posts
    assert len(alice.posts) == 3


class _ShelfBase(DeclarativeBase):
    pass


class Shelf(_ShelfBase):
    __tablename__ = "shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    kept: Mapped[list["Book"]] = relationship(
        primaryjoin="and_(Shelf.id == Book.shelf_id, Book.deleted.is_(False))",
        viewonly=True,
    )
    by_title: Mapped[list["Book"]] = relationship(
        order_by="Book.title.desc()", viewonly=True
    )


class Book(_ShelfBase):
    __tablename__ = "book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("shelf.id"))
    title: Mapped[str] = mapped_column(String(50))
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class TestARelationshipsOwnJoinAndOrder:
    """A ``primaryjoin`` that filters and a ``relationship(order_by=)`` both hold."""

    @pytest.fixture
    def session(self):
        engine = create_engine("sqlite://")
        _ShelfBase.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        session.add_all([Shelf(id=1, name="A"), Shelf(id=2, name="B")])
        session.flush()
        session.add_all(
            [
                Book(id=1, shelf_id=1, title="Alpha"),
                Book(id=2, shelf_id=1, title="Beta", deleted=True),
                Book(id=3, shelf_id=1, title="Gamma"),
                Book(id=4, shelf_id=2, title="Delta"),
            ]
        )
        session.commit()
        yield session
        session.close()
        engine.dispose()

    def _run(self, session, read, scope=None):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            session_getter=lambda info: session,
        )

        @orm.type(Book)
        class BookNode:
            id: auto
            title: auto

        @orm.type(Shelf)
        class ShelfNode:
            id: auto
            kept: list[BookNode] = orm.field.eager(scope=scope)
            by_title: list[BookNode]

        @strawberry.type
        class Query:
            @strawberry.field
            def shelves(self, info: strawberry.Info) -> list[ShelfNode]:
                return read()

        result = orm.schema(query=Query).execute_sync(
            "{ shelves { kept { title } byTitle { title } } }"
        )
        assert result.errors is None, result.errors
        return [
            {key: [book["title"] for book in shelf[key]] for key in shelf}
            for shelf in result.data["shelves"]
        ]

    def test_rows_a_resolver_hands_back(self, session):
        found = self._run(
            session,
            lambda: session.scalars(select(Shelf).order_by(Shelf.id)).all(),
        )
        assert [sorted(shelf["kept"]) for shelf in found] == [
            ["Alpha", "Gamma"],
            ["Delta"],
        ]
        assert [shelf["byTitle"] for shelf in found] == [
            ["Gamma", "Beta", "Alpha"],
            ["Delta"],
        ]

    def test_an_ordered_scope_on_a_filtered_join(self, session):
        found = self._run(
            session,
            lambda: select(Shelf).order_by(Shelf.id),
            scope=lambda query, info: query.order_by(Book.title.desc()),
        )
        assert found == [
            {"kept": ["Gamma", "Alpha"], "byTitle": ["Gamma", "Beta", "Alpha"]},
            {"kept": ["Delta"], "byTitle": ["Delta"]},
        ]
