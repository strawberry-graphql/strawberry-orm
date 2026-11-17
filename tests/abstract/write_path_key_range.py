"""An integer no key column holds is not a valid id.

A ref-list item or an upsert naming one reached the driver, which refused to
bind it with a message of its own (``Python int too large to convert to SQLite
INTEGER``). It is refused like an id that is not a number at all, and a value
too large for its column is refused like any write the database refuses.
"""

from typing import Any

import pytest
import strawberry

from strawberry_orm import UpsertNotFoundError, UpsertValidationError
from tests.abstract.write_path_authorization import (
    Blog,
    TagUpdate,
    item,
    member,
    tag_names,
)

REFUSED = "The database refused the write"
PAST_ANY_KEY = ["9223372036854775808", "-9223372036854775809", "9" * 23]


@strawberry.input
class CommentCreate:
    body: str
    author_id: int


async def _refusal(write: Any) -> str:
    with pytest.raises(UpsertValidationError) as refused:
        await write()
    return str(refused.value)


class AbstractTestKeysPastAnyColumn(Blog):
    @pytest.mark.parametrize("key", PAST_ANY_KEY)
    async def test_a_ref_list_item_naming_one_is_not_a_valid_id(
        self, writes, blog, key
    ):
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Tag, update=TagUpdate)

        def update(pk: str) -> Any:
            refs = [item(ref, "update", id=pk, name="renamed")]
            return writes.refs(orm, blog.hello, "tags", refs)

        not_a_number = await _refusal(lambda: update("abc"))
        assert await _refusal(lambda: update(key)) == not_a_number.replace(
            "'abc'", repr(key)
        )
        assert await tag_names(writes) == ["python", "rust", "go"]

    @pytest.mark.parametrize("key", PAST_ANY_KEY)
    async def test_an_upsert_matching_one_is_not_a_valid_id(self, writes, blog, key):
        orm = writes.new_orm()
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Post,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                }
            },
            name="KeyedPostUpsert",
        )

        def matching(pk: str) -> Any:
            payload = upsert(
                where=member(upsert, "where")(id=pk),
                update=member(upsert, "update")(title="renamed"),
            )
            return writes.upsert(orm, payload)

        not_a_number = await _refusal(lambda: matching("abc"))
        assert await _refusal(lambda: matching(key)) == not_a_number.replace(
            "'abc'", repr(key)
        )
        assert (await writes.reload(blog.hello)).title == "Hello"

    async def test_the_widest_key_is_still_looked_up(self, writes, blog):
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Tag, delete=True)
        refs = [item(ref, "delete", id=str(2**63 - 1))]
        with pytest.raises(UpsertNotFoundError):
            await writes.refs(orm, blog.hello, "tags", refs)

    async def test_a_value_past_its_column_is_refused(self, writes, blog):
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Comment, create=CommentCreate)
        refs = [item(ref, "create", body="fresh", author_id=2**63)]
        with pytest.raises(ValueError, match=REFUSED):
            await writes.refs(orm, blog.hello, "comments", refs)
        comments = await writes.related(blog.hello, "comments")
        assert [comment.body for comment in comments] == ["mine", "reply", "loose"]
