"""Upsert lookups through Django multi-table children, proxies and named parent links."""

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
from tests.backends.django.test_write_path_subclass_rows import (
    WaDoc,
    WaDraftDoc,
    WaNote,
    WaSealedNote,
    writes,  # noqa: F401
)


class TestWriteLookupScopes(AbstractTestWriteLookupScopes):
    pass


class TestSiblingLookupScopes(AbstractTestSiblingLookupScopes):
    pass


@pytest.mark.parametrize("lookup", LOOKUPS)
async def test_a_lookup_through_a_proxy_leaves_out_a_row_its_base_hides(writes, lookup):  # noqa: F811
    """Nothing in the database tells a proxy's rows apart, so every doc is a
    draft doc, and the docs' repo governs each."""
    if LOOKUPS[lookup]:
        await writes.create(WaDoc, id=VISIBLE, title=TITLE)
    await writes.create(WaDoc, id=HIDDEN, title=TITLE)
    calls = []
    orm = writes.new_orm(
        repos={
            WaDoc: recorder(writes, calls, "doc", [VISIBLE]),
            WaDraftDoc: recorder(writes, calls, "draft", None),
        }
    )
    where = where_of(lookup)
    write = writes.upsert(orm, rename(by(orm, WaDraftDoc, where), where))
    await check_missing(writes, write, lookup, calls, WaDoc)


@pytest.mark.parametrize("lookup", LOOKUPS)
async def test_a_named_parent_link_leaves_out_a_row_its_parent_hides(writes, lookup):  # noqa: F811
    """``WaSealedNote`` reaches its parent's key through ``base``, not the
    ``wanote_ptr`` Django would name the link."""
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
