"""A repo's ``scope_query`` allows the same rows wherever a write looks one up.

A write finds each row it touches through the repo: by key for an update, a
delete, an unlink or a nested update, and by the ``where`` fields for an
upsert. Each lookup takes at most a row or two of what it finds, so a scope
limiting the rows to none had its limit replaced by the lookup's own, and the
write went ahead on a row the repo hides. One limiting them to some, or
skipping some, picks rows by where they fall among the lookup's matches.

Each lookup is crossed with each shape of scope. One allowing none leaves
every row as it was; one that limits or offsets is refused, and leaves every
row as it was too.

Each backend supplies a ``writes`` fixture: a
:class:`~tests.abstract.write_path_authorization.WriteDb`.
"""

from contextlib import suppress
from typing import Any

import pytest

from strawberry_orm import AbstractRepo, UpsertNotFoundError, UpsertValidationError
from tests.abstract.write_path_authorization import (
    POST_WITH_AUTHOR,
    Blog,
    TagUpdate,
    allow,
    item,
    member,
)


def bounded(query: Any, limit: int, offset: int = 0) -> Any:
    """*query* limited to *limit* rows after *offset*, as its ORM spells it."""
    if hasattr(query, "limit"):
        query = query.limit(limit)
        return query.offset(offset) if offset else query
    return query[offset : offset + limit]


SHAPES = {
    "limit to none": lambda query: bounded(query, 0),
    "limit to one": lambda query: bounded(query, 1),
    "offset": lambda query: bounded(query, 5, 1),
}

#: The model whose repo each write looks its row up through.
WRITES = {
    "ref update": "Tag",
    "ref delete": "Tag",
    "ref unlink": "Tag",
    "upsert match": "Post",
    "nested update": "User",
}


def shaped_repo(shape: str) -> type:
    class Shaped(AbstractRepo):
        def scope_query(self, query, info):
            return SHAPES[shape](query)

    return Shaped


class AbstractTestRepoScopeShapes(Blog):
    @staticmethod
    async def state(writes: Any, blog: Any) -> Any:
        m = writes.m
        return (
            [(tag.id, tag.name) for tag in await writes.rows(m.Tag)],
            [tag.id for tag in await writes.related(blog.hello, "tags")],
            [(post.id, post.title) for post in await writes.rows(m.Post)],
            [(user.id, user.name) for user in await writes.rows(m.User)],
        )

    @staticmethod
    async def attempt(writes: Any, blog: Any, orm: Any, write: str) -> None:
        m = writes.m
        if write.startswith("ref "):
            operation = write.removeprefix("ref ")
            ref = orm.ref(m.Tag, update=TagUpdate, unlink=True, delete=True)
            values = {"name": "pwned"} if operation == "update" else {}
            await writes.refs(
                orm,
                blog.hello,
                "tags",
                [item(ref, operation, id="1", **values)],
                authorize=allow,
            )
            return
        upsert = orm.mutations.upsert_node_input(
            model=m.Post, project=POST_WITH_AUTHOR, name="ShapedPostUpsert"
        )
        update = member(upsert, "update")
        if write == "upsert match":
            changes = update(title="pwned")
        else:
            link_type = member(upsert, "update", "author")
            author = member(link_type, "update")(id="1", name="pwned")
            changes = update(author=link_type(update=author))
        payload = upsert(where=member(upsert, "where")(title="Hello"), update=changes)
        await writes.upsert(orm, payload, authorize=allow)

    @pytest.mark.parametrize("shape", list(SHAPES))
    @pytest.mark.parametrize("write", list(WRITES))
    async def test_a_write_finds_only_what_the_scope_allows(
        self, writes, blog, write, shape
    ):
        model = getattr(writes.m, WRITES[write])
        orm = writes.new_orm(repos={model: shaped_repo(shape)})
        before = await self.state(writes, blog)

        if shape == "limit to none":
            with suppress(UpsertNotFoundError, UpsertValidationError):
                await self.attempt(writes, blog, orm, write)
        else:
            with pytest.raises(ValueError, match="cannot limit or offset"):
                await self.attempt(writes, blog, orm, write)

        assert await self.state(writes, blog) == before
