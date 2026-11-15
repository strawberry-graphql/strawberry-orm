"""A relation another execution loaded on a shared Session is not trusted.

The identity map hands every execution using one Session the same objects, so
a relation one request's scope loaded sits on the rows another request reads.
A load marks the relations it filled as read with its scope, and a mark holds
only for that execution. With an ``AsyncSession`` or ``offload_sync_session``
two executions' statements interleave, so what one load filled has to be told
from what another filled meanwhile, and a mark from a value put there since.
"""

import contextvars
from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from strawberry_orm import StrawberryORM
from strawberry_orm.lazy_resolution import execution_scope, mark_rows_scoped
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Base, Post, User

QUERY = "{ users { name posts { title } } }"


@pytest.fixture
def sa_session(tmp_path):
    # A file database: in-memory SQLite gives each thread its own.
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _orm(**kwargs):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **kwargs
    )


def _schema(orm, *, before_posts=None):
    @orm.type(Post)
    class PostType:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Post.is_published.is_(True))

    @orm.type(User)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

        @strawberry.field
        def touched(self, info: strawberry.Info) -> bool:
            before_posts(self, info.context["session"])
            return True

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.eager()

    return orm.schema(query=Query)


def _as_another_request(work):
    def run():
        with execution_scope():
            work()

    contextvars.copy_context().run(run)


def _every_post(session):
    return session.scalars(select(User).options(selectinload(User.posts))).all()


def _posts_by_user(result):
    assert result.errors is None, result.errors
    return {u["name"]: [p["title"] for p in u["posts"]] for u in result.data["users"]}


SCOPED = {
    "Alice": ["Hello World", "GraphQL Guide"],
    "Bob": [],
    "Charlie": ["Rust Adventures"],
}


def test_a_relation_another_request_loaded_mid_query_is_read_again(sa_session, seed):
    loaded = []

    def interleave(state):
        if not loaded and not state.is_relationship_load:
            loaded.append(True)
            _as_another_request(lambda: _every_post(sa_session))

    event.listen(sa_session, "do_orm_execute", interleave)
    result = _schema(_orm()).execute_sync(QUERY, context_value={"session": sa_session})
    assert loaded
    assert _posts_by_user(result) == SCOPED


def test_a_relation_replaced_after_it_was_loaded_is_read_again(sa_session, seed):
    def replace(user, session):
        def load():
            every = session.scalars(select(Post).where(Post.author_id == user.id))
            set_committed_value(user, "posts", list(every))

        _as_another_request(load)

    schema = _schema(_orm(), before_posts=replace)
    query = "{ users { name touched posts { title } } }"
    result = schema.execute_sync(query, context_value={"session": sa_session})
    assert _posts_by_user(result) == SCOPED


async def test_a_row_scoped_before_another_request_marked_it_is_kept(sa_session, seed):
    orm = _orm(offload_sync_session=True)
    _schema(orm)
    backend = orm._backend
    info = SimpleNamespace(context={"session": sa_session})
    hello, guide = sa_session.get(Post, 1), sa_session.get(Post, 2)
    with execution_scope():
        mark_rows_scoped([hello])
        pending = backend.scope_instances([hello, guide], info)
        _as_another_request(lambda: mark_rows_scoped([hello]))
        assert await pending == [hello, guide]
