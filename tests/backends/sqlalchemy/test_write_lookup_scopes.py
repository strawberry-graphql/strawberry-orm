"""Upsert lookups through SQLAlchemy joined-table subclasses without a discriminator."""

import pytest

from tests.abstract.write_lookup_scopes import (
    HIDDEN,
    LOOKUPS,
    TITLE,
    VISIBLE,
    AbstractTestSiblingLookupScopes,
    AbstractTestWriteLookupScopes,
    by,
    check_missing,
    rename,
    where_of,
)
from tests.abstract.write_path_subclass_rows import recorder
from tests.backends.sqlalchemy.test_write_path_subclass_rows import (
    WaNote,
    WaSealedNote,
    writes,  # noqa: F401
)


class TestWriteLookupScopes(AbstractTestWriteLookupScopes):
    pass


class TestSiblingLookupScopes(AbstractTestSiblingLookupScopes):
    pass


@pytest.mark.parametrize("lookup", LOOKUPS)
async def test_a_key_named_apart_leaves_out_a_row_its_base_hides(writes, lookup):  # noqa: F811
    """``WaSealedNote`` keys its own table by ``note_id``, not its base's ``id``."""
    if LOOKUPS[lookup]:
        await writes.create(WaSealedNote, id=VISIBLE, title=TITLE)
    await writes.create(WaSealedNote, id=HIDDEN, title=TITLE)
    calls = []
    orm = writes.new_orm(
        repos={
            WaNote: recorder(writes, calls, "note", [VISIBLE]),
            WaSealedNote: recorder(writes, calls, "sealed", None),
        }
    )
    where = where_of(lookup)
    write = writes.upsert(orm, rename(by(orm, WaSealedNote, where), where))
    await check_missing(writes, write, lookup, calls, WaNote)
