"""No value a client sends reaches the warnings registry.

Python keeps every distinct warning text it shows in ``__warningregistry__``
for the life of the process, and logs it. Some of the ORMs' warnings quote a
value: with time zones on, Django and Tortoise warn about a naive datetime as
it reaches a column, and SQLAlchemy warns about a new row taking the key of a
row its session holds, naming both by address. So every datetime a client
sent, and every such create, was a new entry, never freed. A datetime is read
in the default time zone before the ORM sees it, as the ORM would have read
it, wherever it is going - a filter, the lookup an upsert finds its row by, a
row a write saves - and a create taking a held row's key is refused, as the
database would have refused it.

Each backend supplies a ``writes`` fixture, a
:class:`~tests.abstract.write_path_authorization.WriteDb`, and on the test
class:

- ``time_zones()``, a context manager under which time zone support is on;
- ``stored(stamp)``, the value a column holds once the naive *stamp* is
  written there.
"""

import datetime
from contextlib import contextmanager
from typing import Any

import pytest
import strawberry

from strawberry_orm import UpsertNotFoundError
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import Blog, allow, item, member
from tests.recorded_warnings import recording

STAMPS = [datetime.datetime(2001, 2, 3, 4, 5, second) for second in range(3)]

CONDITIONS = {
    "exact": '{exact: "%s"}',
    "neq": '{neq: "%s"}',
    "gt": '{gt: "%s"}',
    "gte": '{gte: "%s"}',
    "lt": '{lt: "%s"}',
    "lte": '{lte: "%s"}',
    "inList": '{inList: ["%s"]}',
    "notInList": '{notInList: ["%s"]}',
    "range": '{range: {start: "%s", end: "2999-01-01T00:00:00"}}',
}

POST_STAMPS = {
    "_meta": {
        "update": ["created_at"],
        "upsert": {"where": ["title"], "onMatch": "UPDATE"},
    },
    "author": {
        "_meta": {
            "update": ["created_at"],
            "upsert": {"where": ["id"], "onMatch": "IGNORE"},
        }
    },
}

POST_BY_STAMP = {
    "_meta": {
        "update": ["title"],
        "upsert": {"where": ["created_at"], "onMatch": "UPDATE"},
    }
}

USER_STAMPS = {
    "_meta": {
        "create": ["name", "email", "created_at"],
        "upsert": {"where": ["name"], "onMatch": "IGNORE"},
    }
}

#: The writes that change Hello's ``created_at``, or its author's.
UPDATES = {
    "an upsert's update": "hello",
    "a ref list's update": "hello",
    "a nested update": "alice",
}

WRITES = ["an upsert's lookup", "an upsert's insert", "a ref list's create", *UPDATES]


@strawberry.input
class PostStampCreate:
    title: str
    body: str
    created_at: datetime.datetime


@strawberry.input
class PostStampUpdate:
    id: strawberry.ID
    created_at: datetime.datetime | None = strawberry.UNSET


@strawberry.input
class PostKeyCreate:
    """A create input the app wrote by hand, which offers the key."""

    id: int
    title: str
    body: str


@contextmanager
def no_warnings():
    with recording() as caught:
        yield
    messages = [str(warning.message) for warning in caught]
    assert not messages, messages


def _post_schema(writes: Any) -> Any:
    orm = writes.new_orm()
    Post = writes.m.Post

    @orm.type(Post, filters=orm.filter(Post))
    class CvPost:
        id: auto
        created_at: auto

    @strawberry.type
    class Query:
        posts: list[CvPost] = orm.field.eager()

    return orm.schema(query=Query)


class AbstractTestClientValuesInWarnings(Blog):
    @staticmethod
    async def write(writes: Any, blog: Any, how: str, i: int, stamp: Any) -> None:
        """Make the write *how*, the *i*-th time, with the datetime *stamp*."""
        m = writes.m
        orm = writes.new_orm()
        if how == "an upsert's lookup":
            upsert = orm.mutations.upsert_node_input(
                model=m.Post, project=POST_BY_STAMP, name="CvPostByStamp"
            )
            payload = upsert(where=member(upsert, "where")(created_at=stamp))
            with pytest.raises(UpsertNotFoundError):
                await writes.upsert(orm, payload, authorize=allow)
        elif how == "an upsert's insert":
            upsert = orm.mutations.upsert_node_input(
                model=m.User, project=USER_STAMPS, name="CvUserStamps"
            )
            name = f"new {i}"
            payload = upsert(
                where=member(upsert, "where")(name=name),
                create=member(upsert, "create")(
                    name=name, email=f"{i}@example.com", created_at=stamp
                ),
            )
            await writes.upsert(orm, payload, authorize=allow)
        elif how == "a ref list's create":
            ref = orm.ref(m.Post, create=PostStampCreate)
            made = item(ref, "create", title=f"new {i}", body="B", created_at=stamp)
            await writes.refs(orm, blog.alice, "posts", [made], authorize=allow)
        elif how == "a ref list's update":
            ref = orm.ref(m.Post, update=PostStampUpdate)
            refs = [item(ref, "update", id="1", created_at=stamp)]
            await writes.refs(orm, blog.alice, "posts", refs, authorize=allow)
        else:
            upsert = orm.mutations.upsert_node_input(
                model=m.Post, project=POST_STAMPS, name="CvPostStamps"
            )
            update = member(upsert, "update")
            if how == "a nested update":
                link_type = member(update, "author")
                author = member(link_type, "update")(id="1", created_at=stamp)
                changes = update(author=link_type(update=author))
            else:
                changes = update(created_at=stamp)
            payload = upsert(
                where=member(upsert, "where")(title="Hello"), update=changes
            )
            await writes.upsert(orm, payload, authorize=allow)

    @pytest.mark.parametrize("operator", list(CONDITIONS))
    async def test_a_datetime_a_client_filters_by(self, writes, blog, operator):
        schema = _post_schema(writes)
        with self.time_zones(), no_warnings():
            for stamp in STAMPS:
                condition = CONDITIONS[operator] % stamp.isoformat()
                result = await writes.execute(
                    schema,
                    f"{{ posts(filter: {{field: {{createdAt: {condition}}}}}) {{ id }} }}",
                )
                assert result.errors is None, result.errors

    @pytest.mark.parametrize("how", WRITES)
    async def test_a_datetime_a_client_writes(self, writes, blog, how):
        with self.time_zones(), no_warnings():
            for i, stamp in enumerate(STAMPS):
                await self.write(writes, blog, how, i, stamp)
            if how in UPDATES:
                row = await writes.reload(getattr(blog, UPDATES[how]))
                assert row.created_at == self.stored(STAMPS[-1])

    async def test_a_create_taking_the_key_of_a_row_the_session_holds(
        self, writes, blog
    ):
        before = [(post.id, post.title) for post in await writes.rows(writes.m.Post)]
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Post, create=PostKeyCreate)
        with self.time_zones(), no_warnings():
            for _ in range(3):
                made = item(ref, "create", id=1, title="pwned", body="B")
                with pytest.raises(
                    ValueError, match="^The database refused the write$"
                ):
                    await writes.refs(orm, blog.alice, "posts", [made], authorize=allow)
        after = [(post.id, post.title) for post in await writes.rows(writes.m.Post)]
        assert after == before
