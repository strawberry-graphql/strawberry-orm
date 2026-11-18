"""A statement probe leaves nothing behind, however its resolver ends.

The lazy-resolution extension counts the statements each resolver on an ORM
row issues. A backend counts them with a hook on something every request
shares - Tortoise's default client, Django's connection - so a probe that
installs a hook per resolver and takes it out again must take out its own,
and must be closed even when the resolver's coroutine never runs: GraphQL
drops a row's pending fields when a sibling field fails. A probe left open,
or one that took out another's hook, kept counting every statement of every
later request, and a layer more was left for each overlap until no
statement could run at all.

Each way a resolver's run can end - one after another, sibling rows awaited
together, two requests interleaved, a resolver raising, a pending field
dropped, a request cancelled - is crossed with two checks: every probe the
extension opened was closed, and what the backend installed on shared
objects is what it was before.

A backend provides ``make_orm(**options)``, ``models`` (``Post``),
``run(schema, query, context)`` executing asynchronously, ``hooks()``: what
is installed on the shared objects a probe counts with, and ``many_posts``,
which adds 1,200 posts to the seeded ones.
"""

import asyncio
import gc
from contextlib import contextmanager

import pytest
import strawberry

from strawberry_orm.types import auto


def _schema(make_orm, models):
    orm = make_orm(warn_missing_scope=False, lazy_resolution="warn")

    @orm.type(models.Post)
    class ProbedPost:
        id: auto
        title: auto

        @strawberry.field
        async def shout(self) -> str:
            await asyncio.sleep(0)
            return self.title.upper()

        @strawberry.field
        async def held(self, info: strawberry.Info) -> str:
            info.context["entered"].set()
            await info.context["release"].wait()
            return self.title

        @strawberry.field
        async def fails(self) -> str:
            await asyncio.sleep(0)
            raise ValueError("fails")

        @strawberry.field
        def boom(self) -> str:
            raise ValueError("boom")

    @strawberry.type
    class Query:
        posts: list[ProbedPost] = orm.field.eager()

    return orm, orm.schema(query=Query)


def _tracked(orm) -> tuple[list, list]:
    """The probes *orm*'s extension has open, and those it closed with their
    counts at the time."""
    backend, opened, closed = orm.backend, [], []
    real = backend.query_probe

    class Tracked:
        def __init__(self, info):
            self.inner = real(info)

        def __enter__(self):
            opened.append(self)
            self.probe = self.inner.__enter__()
            return self.probe

        def __exit__(self, *exc):
            opened.remove(self)
            try:
                return self.inner.__exit__(*exc)
            finally:
                closed.append((self.probe, self.probe.count))

    backend.query_probe = Tracked
    return opened, closed


def _gate():
    return {"entered": asyncio.Event(), "release": asyncio.Event()}


@contextmanager
def _collecting_only_new():
    """``gc.collect()`` inside walks only the objects made inside, not every
    earlier test's."""
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()


async def _settle():
    """Let the rows GraphQL went on resolving after a failure finish."""
    for _ in range(10):
        await asyncio.sleep(0)
    gc.collect()


async def _one_after_another(schema, run):
    for _ in range(3):
        result = await run(schema, "{ posts { id shout } }", {})
        assert result.errors is None, result.errors


async def _siblings(schema, run):
    result = await run(schema, "{ a: posts { shout } b: posts { shout } }", {})
    assert result.errors is None, result.errors


async def _interleaved(schema, run):
    query = "{ posts { held } }"
    a, b = _gate(), _gate()
    first = asyncio.create_task(run(schema, query, a))
    await a["entered"].wait()
    second = asyncio.create_task(run(schema, query, b))
    await b["entered"].wait()
    a["release"].set()
    assert (await first).errors is None
    assert (await run(schema, "{ posts { id title } }", {})).errors is None
    b["release"].set()
    assert (await second).errors is None


async def _raising(schema, run):
    result = await run(schema, "{ posts { id fails } }", {})
    assert result.errors, "the resolver's error was not reported"


async def _dropped(schema, run):
    with _collecting_only_new():
        result = await run(schema, "{ posts { shout boom } }", {})
        assert result.errors, "the failing sibling was not reported"
        await _settle()


async def _cancelled(schema, run):
    with _collecting_only_new():
        gate = _gate()
        task = asyncio.create_task(run(schema, "{ posts { held } }", gate))
        await gate["entered"].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()


ENDINGS = {
    "one after another": _one_after_another,
    "siblings": _siblings,
    "interleaved": _interleaved,
    "raising": _raising,
    "dropped": _dropped,
    "cancelled": _cancelled,
}


class AbstractTestProbeLifetime:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_every_probe_opened_is_closed(self, make_orm, models, run, ending):
        orm, schema = _schema(make_orm, models)
        opened, _ = _tracked(orm)
        await ENDINGS[ending](schema, run)
        assert not opened, f"{len(opened)} probes were left open after {ending}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_a_closed_probe_counts_nothing_more(
        self, make_orm, models, run, ending
    ):
        orm, schema = _schema(make_orm, models)
        _, closed = _tracked(orm)
        await ENDINGS[ending](schema, run)
        await _one_after_another(schema, run)
        assert closed, "no probe was opened"
        grown = [
            (count, probe.count) for probe, count in closed if probe.count != count
        ]
        assert not grown, f"after {ending}, closed probes went on counting: {grown}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", list(ENDINGS))
    async def test_nothing_is_left_on_shared_objects(
        self, make_orm, models, run, hooks, ending
    ):
        _, schema = _schema(make_orm, models)
        await _one_after_another(schema, run)
        before = hooks()
        for _ in range(3):
            await ENDINGS[ending](schema, run)
        assert hooks() == before, f"{ending} left {hooks()} where there was {before}"

    @pytest.mark.asyncio
    async def test_a_list_of_many_rows_leaves_every_statement_able_to_run(
        self, make_orm, models, run, many_posts
    ):
        _, schema = _schema(make_orm, models)
        for query in ("{ posts { shout } }", "{ posts { id } }"):
            result = await run(schema, query, {})
            assert result.errors is None, f"{query} failed with {result.errors}"
