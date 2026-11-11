"""How the server runs an operation: awaited, in a worker thread, or your own way."""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest
import strawberry
from django.core.exceptions import SynchronousOnlyOperation
from graphql import GraphQLError
from mcp import Client
from strawberry.types.graphql import OperationType

from strawberry_orm.contrib.mcp import execution, mcp_server
from strawberry_orm.contrib.mcp.execution import Executor

from .fakes import schema

QUERY = {OperationType.QUERY}
MUTATION = {OperationType.MUTATION}
POSTS = "{ posts { title } }"
TITLES = {"posts": [{"title": "Hello"}]}


def _counting_schema():
    """A schema whose resolvers count their runs, then refuse the event loop
    as a Django resolver that does work before its first ORM call would."""
    runs = {"count": 0, "bump": 0}

    def refuse_the_loop(key):
        runs[key] += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return runs[key]
        raise SynchronousOnlyOperation("You cannot call this from an async context")

    @strawberry.type
    class Query:
        @strawberry.field
        def count(self) -> int:
            return refuse_the_loop("count")

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        def bump(self) -> int:
            return refuse_the_loop("bump")

    return strawberry.Schema(query=Query, mutation=Mutation), runs


async def test_a_sync_query_is_rerun_off_the_event_loop_and_warned_about(caplog):
    counting, runs = _counting_schema()
    executor = Executor(counting, sync=None, execute=None)

    with (
        caplog.at_level(logging.WARNING, logger=execution.__name__),
        pytest.warns(UserWarning, match="execute_sync in a worker thread"),
    ):
        first = await executor.run("{ count }", {}, None, QUERY)
    second = await executor.run("{ count }", {}, None, QUERY)

    assert first.data == {"count": 2}
    assert second.data == {"count": 3}
    assert executor.sync_execute
    assert any("execute_sync" in record.message for record in caplog.records)


async def test_a_sync_mutation_is_not_run_twice():
    counting, runs = _counting_schema()
    server = mcp_server(counting, operations="mutation Bump { bump }")

    async with Client(server) as client:
        result = await client.call_tool("Bump", {})

    assert result.is_error is True
    assert "Pass sync=True" in result.content[0].text
    assert runs["bump"] == 1


async def test_sync_true_runs_a_mutation_once_off_the_event_loop():
    counting, runs = _counting_schema()
    server = mcp_server(counting, operations="mutation Bump { bump }", sync=True)

    async with Client(server) as client:
        result = await client.call_tool("Bump", {})

    assert result.structured_content == {"data": {"bump": 1}}
    assert runs["bump"] == 1


async def test_sync_true_makes_no_async_attempt():
    class SyncOnly:
        async def execute(self, *args, **kwargs):
            raise AssertionError("execute was attempted")

        def execute_sync(self, *args, **kwargs):
            return schema.execute_sync(*args, **kwargs)

    executor = Executor(SyncOnly(), sync=True, execute=None)

    result = await executor.run(
        '{ posts(filter: {title: "Hello"}) { title } }', {}, {}, QUERY
    )

    assert result.data == TITLES


async def test_sync_false_never_falls_back():
    counting, runs = _counting_schema()
    executor = Executor(counting, sync=False, execute=None)

    result = await executor.run("{ count }", {}, None, QUERY)

    assert result.data is None
    assert "async context" in result.errors[0].message
    assert runs["count"] == 1
    assert not executor.sync_execute


class _Raising:
    def __init__(self, error):
        self.error = error

    async def execute(self, *args, **kwargs):
        raise self.error

    def execute_sync(self, *args, **kwargs):
        return schema.execute_sync(*args, **kwargs)


async def test_a_raised_sync_only_operation_also_falls_back():
    executor = Executor(
        _Raising(SynchronousOnlyOperation("async unsafe")), sync=None, execute=None
    )

    with pytest.warns(UserWarning, match="execute_sync"):
        result = await executor.run(POSTS, {}, {}, QUERY)

    assert result.data["posts"][0] == {"title": "Hello"}


async def test_sync_false_lets_a_raised_sync_only_operation_through():
    executor = Executor(
        _Raising(SynchronousOnlyOperation("async unsafe")), sync=False, execute=None
    )

    with pytest.raises(SynchronousOnlyOperation):
        await executor.run(POSTS, {}, {}, QUERY)


async def test_other_errors_still_raise():
    executor = Executor(_Raising(RuntimeError("db down")), sync=None, execute=None)

    with pytest.raises(RuntimeError, match="db down"):
        await executor.run(POSTS, {}, {}, QUERY)


async def test_an_execute_hook_runs_every_operation_and_cannot_widen_it():
    seen = []

    async def execute(query, variables, context, allowed):
        seen.append((query, variables, context, set(allowed)))
        allowed.update(OperationType)
        return await schema.execute(
            query, variable_values=variables, context_value=context
        )

    executor = Executor(schema, sync=None, execute=execute)
    allowed = set(QUERY)

    result = await executor.run(POSTS, {"a": 1}, {"user": "alice"}, allowed)

    assert result.data["posts"][0] == {"title": "Hello"}
    assert seen == [(POSTS, {"a": 1}, {"user": "alice", "seen": ["alice"]}, QUERY)]
    assert allowed == QUERY


def test_execute_and_sync_cannot_both_be_given():
    with pytest.raises(ValueError, match="not both"):
        Executor(schema, sync=True, execute=lambda *a: None)


async def test_sync_execution_closes_stale_connections_around_each_call(monkeypatch):
    events = []
    loop_thread = threading.get_ident()

    def close():
        events.append(("close", threading.get_ident() != loop_thread))

    class Recording:
        def execute_sync(self, *args, **kwargs):
            events.append(("execute", threading.get_ident() != loop_thread))
            return schema.execute_sync(*args, **kwargs)

    monkeypatch.setattr(execution, "_django_close_old_connections", lambda: close)
    executor = Executor(Recording(), sync=True, execute=None)

    await executor.run(POSTS, {}, {}, QUERY)

    assert events == [("close", True), ("execute", True), ("close", True)]


def _no_django(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_django(name, *args, **kwargs):
        if name.split(".", 1)[0] == "django":
            raise ImportError("no django")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_django)


def test_close_old_connections_needs_a_configured_django(monkeypatch):
    import django.conf
    import django.db

    monkeypatch.setattr(
        django.conf.LazySettings, "configured", property(lambda self: False)
    )
    assert execution._django_close_old_connections() is execution._no_op
    assert execution._no_op() is None

    monkeypatch.setattr(
        django.conf.LazySettings, "configured", property(lambda self: True)
    )
    assert execution._django_close_old_connections() is django.db.close_old_connections

    _no_django(monkeypatch)
    assert execution._django_close_old_connections() is execution._no_op


def test_without_django_nothing_is_a_sync_only_operation(monkeypatch):
    _no_django(monkeypatch)
    wrapped = GraphQLError("x", original_error=RuntimeError("x"))

    assert execution._is_sync_only_operation(Exception("x")) is False
    assert (
        execution._result_has_sync_only_operation(SimpleNamespace(errors=[wrapped]))
        is False
    )
    assert execution._result_has_sync_only_operation(SimpleNamespace()) is False
