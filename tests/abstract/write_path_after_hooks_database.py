"""``on_after_*`` hooks wait for the database to accept the write too.

The hooks run once every check has passed, but the database checks some of a
write only later: SQLAlchemy flushes the last links as the savepoint is
released, and Django's foreign keys on SQLite and PostgreSQL are checked as the
transaction commits. A hook that ran first sent its mail for a write the
database then refused.

Each backend's ``test_write_path_after_hooks_database.py`` reuses the
``writes`` fixture of its ``test_write_path_authorization.py`` and supplies:

- ``refusing``: the database refuses to tag a post rust (tag 2), and to move
  comment 2 to another post;
- ``keys_enforced``: the database refuses a key that names no row.
"""

from typing import Any

import pytest
import strawberry

from strawberry_orm import AbstractRepo
from tests.abstract.write_path_authorization import (
    Blog,
    CommentUpdate,
    TagCreate,
    TagUpdate,
    ids,
    item,
    label,
    member,
    names,
    tag_names,
)

REFUSED = "The database refused the write"


def refusing_triggers(post_tags: str) -> list[str]:
    """SQLite triggers refusing what ``refusing`` refuses; *post_tags* links tags."""
    return [
        f"CREATE TRIGGER refuse_rust BEFORE INSERT ON {post_tags} "
        "WHEN NEW.tag_id = 2 BEGIN SELECT RAISE(ABORT, 'refused'); END",
        "CREATE TRIGGER refuse_moving_theirs BEFORE UPDATE OF post_id ON comment "
        "WHEN NEW.id = 2 BEGIN SELECT RAISE(ABORT, 'refused'); END",
    ]


DROP_REFUSING_TRIGGERS = [
    "DROP TRIGGER refuse_rust",
    "DROP TRIGGER refuse_moving_theirs",
]


@strawberry.input
class CommentCreate:
    body: str
    author_id: int = 1


def announcing_repo(outbox: list[tuple[str, Any]]) -> type:
    """Sends its news from ``on_after_*``."""

    class AnnouncingRepo(AbstractRepo):
        def on_after_create(self, instance, info):
            outbox.append(("created", label(instance)))

        def on_after_update(self, instance, info):
            outbox.append(("updated", label(instance)))

    return AnnouncingRepo


class AbstractTestAfterHooksWaitForTheDatabase(Blog):
    async def test_a_tag_the_database_refuses_sends_nothing(
        self, writes, blog, refusing
    ):
        outbox: list[tuple[str, Any]] = []
        orm = writes.new_orm(repos={writes.m.Tag: announcing_repo(outbox)})
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate)
        refs = [
            item(ref, "create", name="fresh"),
            item(ref, "update", id="1", name="py"),
            item(ref, "update", id="2"),
        ]
        with pytest.raises(ValueError, match=REFUSED):
            await writes.refs(orm, blog.hello, "tags", refs)
        assert outbox == []
        assert await tag_names(writes) == ["python", "rust", "go"]
        assert await names(writes, blog.hello, "tags") == ["python", "go"]

    async def test_a_move_the_database_refuses_sends_nothing(
        self, writes, blog, refusing
    ):
        outbox: list[tuple[str, Any]] = []
        orm = writes.new_orm(repos={writes.m.Comment: announcing_repo(outbox)})
        ref = orm.ref(writes.m.Comment, create=CommentCreate, update=CommentUpdate)
        refs = [item(ref, "create", body="fresh"), item(ref, "update", id="2")]
        with pytest.raises(ValueError, match=REFUSED):
            await writes.refs(orm, blog.hello, "comments", refs)
        assert outbox == []
        assert await ids(writes, blog.hello, "comments") == [1, 3, 5]
        assert await ids(writes, blog.other, "comments") == [2, 4]

    async def test_an_upsert_the_database_refuses_sends_nothing(
        self, writes, blog, refusing
    ):
        outbox: list[tuple[str, Any]] = []
        m = writes.m
        repo = announcing_repo(outbox)
        orm = writes.new_orm(repos={m.Post: repo, m.Tag: repo})
        upsert = orm.mutations.upsert_node_input(
            model=m.Post,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": ["title"], "onMatch": "UPDATE"},
                },
                "tags": {"_meta": {"create": ["name"], "update": ["name"]}},
            },
            name="AnnouncedPostUpsert",
        )
        tags = member(upsert, "update", "tags")
        payload = upsert(
            where=member(upsert, "where")(title="Hello"),
            update=member(upsert, "update")(
                title="Hello again",
                tags=[item(tags, "create", name="fresh"), item(tags, "update", id="2")],
            ),
        )
        with pytest.raises(ValueError, match=REFUSED):
            await writes.upsert(orm, payload)
        assert outbox == []
        assert (await writes.reload(blog.hello)).title == "Hello"
        assert await tag_names(writes) == ["python", "rust", "go"]

    async def test_a_write_the_database_accepts_still_sends(
        self, writes, blog, refusing
    ):
        outbox: list[tuple[str, Any]] = []
        orm = writes.new_orm(repos={writes.m.Tag: announcing_repo(outbox)})
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate)
        refs = [item(ref, "create", name="fresh"), item(ref, "update", id="1")]
        await writes.refs(orm, blog.other, "tags", refs)
        assert outbox == [("created", "fresh")]
        assert await names(writes, blog.other, "tags") == ["python", "rust", "fresh"]


class AbstractTestAfterHooksWaitForTheKeys(Blog):
    async def test_a_key_naming_no_row_sends_nothing(self, writes, blog, keys_enforced):
        outbox: list[tuple[str, Any]] = []
        orm = writes.new_orm(repos={writes.m.Comment: announcing_repo(outbox)})
        ref = orm.ref(writes.m.Comment, create=CommentCreate)
        refs = [item(ref, "create", body="fresh", author_id=999)]
        with pytest.raises(ValueError, match=REFUSED):
            await writes.refs(orm, blog.hello, "comments", refs)
        assert outbox == []
        assert await ids(writes, blog.hello, "comments") == [1, 3, 5]
