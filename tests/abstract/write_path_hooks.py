"""Repo hooks the write path waits for, and relations it will not treat as lists.

Each backend's ``test_write_path_hooks.py`` reuses the ``writes`` fixture of
its ``test_write_path_authorization.py``: a sync and an async SQLAlchemy
session, Django from sync and async code, and Tortoise.
"""

import gc
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from strawberry_orm import AbstractRepo
from tests.abstract.write_path_authorization import (
    Blog,
    TagCreate,
    TagUpdate,
    item,
    link_recorder,
    member,
    names,
    tag_names,
)

# -- Lifecycle hooks written async ------------------------------------------------


def async_lifecycle_repo(log: list[tuple[str, str]]) -> type:
    """Every lifecycle hook ``async``: names are shouted, ``python`` is kept."""

    class AsyncLifecycleRepo(AbstractRepo):
        async def on_before_create(self, data, info):
            return {**data, "name": data["name"].upper()}

        async def on_after_create(self, instance, info):
            log.append(("created", instance.name))

        async def on_before_update(self, instance, data, info):
            return {**data, "name": data["name"].upper()}

        async def on_after_update(self, instance, info):
            log.append(("updated", instance.name))

        async def on_before_delete(self, instance, info):
            if instance.name == "python":
                raise PermissionError("python cannot be deleted")
            log.append(("deleting", instance.name))

    return AsyncLifecycleRepo


async def refused_on_a_sync_write(write: Callable[[], Awaitable[Any]]) -> None:
    """A sync write refuses an async hook outright, leaving no coroutine behind."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(TypeError, match="returned an awaitable"):
            await write()
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]


class AbstractTestAsyncLifecycleHooks(Blog):
    @pytest.fixture
    def log(self):
        return []

    @pytest.fixture
    def orm(self, writes, log):
        return writes.new_orm(repos={writes.m.Tag: async_lifecycle_repo(log)})

    async def test_an_async_on_before_delete_can_stop_the_delete(
        self, writes, blog, orm
    ):
        ref = orm.ref(writes.m.Tag, delete=True)

        def delete_python():
            return writes.refs(orm, blog.hello, "tags", [item(ref, "delete", id="1")])

        if writes.async_hooks:
            with pytest.raises(PermissionError, match="python cannot be deleted"):
                await delete_python()
        else:
            await refused_on_a_sync_write(delete_python)
        assert await tag_names(writes) == ["python", "rust", "go"]

    async def test_async_hooks_run_around_a_ref_list(self, writes, blog, orm, log):
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate, delete=True)
        refs = [
            item(ref, "create", name="new"),
            item(ref, "update", id="3", name="golang"),
            item(ref, "delete", id="3"),
        ]

        def apply():
            return writes.refs(orm, blog.hello, "tags", refs)

        if not writes.async_hooks:
            await refused_on_a_sync_write(apply)
            assert log == []
            assert await tag_names(writes) == ["python", "rust", "go"]
            return
        await apply()
        # The after hooks wait for the whole list to pass its checks.
        assert log == [
            ("deleting", "GOLANG"),
            ("created", "NEW"),
            ("updated", "GOLANG"),
        ]
        assert await names(writes, blog.hello, "tags") == ["python", "NEW"]

    async def test_async_hooks_run_around_an_upsert(self, writes, blog, orm, log):
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Tag,
            project={
                "_meta": {
                    "create": ["name"],
                    "update": ["name"],
                    "upsert": {"where": ["name"], "onMatch": "UPDATE"},
                }
            },
            name="TagUpsertInput",
        )
        payloads = [
            upsert(
                where=member(upsert, "where")(name="zig"),
                create=member(upsert, "create")(name="zig"),
            ),
            upsert(
                where=member(upsert, "where")(name="rust"),
                update=member(upsert, "update")(name="rustlang"),
            ),
        ]
        if not writes.async_hooks:
            for payload in payloads:
                await refused_on_a_sync_write(
                    lambda payload=payload: writes.upsert(orm, payload)
                )
            assert log == []
            assert await tag_names(writes) == ["python", "rust", "go"]
            return
        for payload in payloads:
            await writes.upsert(orm, payload)
        assert log == [("created", "ZIG"), ("updated", "RUSTLANG")]
        assert await tag_names(writes) == ["python", "RUSTLANG", "go", "ZIG"]


# -- After hooks wait for every check ---------------------------------------------


def notifying_repo(outbox: list[tuple[str, str]]) -> type:
    """Sends its news from ``on_after_*``; a tag named ``spam`` may not be linked."""

    class NotifyingRepo(AbstractRepo):
        def can_link(self, parent, field, instance, info):
            return instance.name != "spam"

        def on_after_create(self, instance, info):
            outbox.append(("created", instance.name))

        def on_after_update(self, instance, info):
            outbox.append(("updated", instance.name))

    return NotifyingRepo


class AbstractTestAfterHooksWaitForTheChecks(Blog):
    async def test_a_write_a_later_check_refuses_sends_nothing(self, writes, blog):
        outbox: list[tuple[str, str]] = []
        orm = writes.new_orm(repos={writes.m.Tag: notifying_repo(outbox)})
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate)
        refs = [
            item(ref, "create", name="fine"),
            item(ref, "update", id="3", name="golang"),
            item(ref, "create", name="spam"),
        ]
        with pytest.raises(PermissionError, match="can_link denied"):
            await writes.refs(orm, blog.hello, "tags", refs)
        assert outbox == []
        assert await tag_names(writes) == ["python", "rust", "go"]

    async def test_a_write_that_passes_sends_in_order(self, writes, blog):
        outbox: list[tuple[str, str]] = []
        orm = writes.new_orm(repos={writes.m.Tag: notifying_repo(outbox)})
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate)
        refs = [
            item(ref, "create", name="fine"),
            item(ref, "update", id="3", name="golang"),
        ]
        await writes.refs(orm, blog.hello, "tags", refs)
        assert outbox == [("created", "fine"), ("updated", "golang")]


# -- apply_ref_list() manages lists ------------------------------------------------


class AbstractTestRefListNeedsAList(Blog):
    async def test_a_single_link_is_refused_before_anything_is_asked(
        self, writes, blog
    ):
        calls: list[tuple[Any, ...]] = []
        orm = writes.new_orm(repos={writes.m.User: link_recorder(calls, locked=set())})
        ref = orm.ref(writes.m.User)
        with pytest.raises(TypeError, match=r"Post\.author links a single row"):
            await writes.refs(orm, blog.hello, "author", [item(ref, "update", id="3")])
        assert calls == []
        assert (await writes.reload(blog.hello)).author_id == 1
