"""Scoping hook call-order tests for Tortoise."""

import builtins

import pytest
import strawberry

from strawberry_orm.types import auto
from tests.abstract.query_scoping_hook_order import (
    SCOPE_PREFIX,
    _posts_load_exclude_guide,
    _published_posts_queryset,
    assert_scope_rows_before_load,
    scope_messages,
)


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query: str):
        return await schema.execute(query)

    return _execute


def _install_print_tracker(monkeypatch) -> list[str]:
    calls: list[str] = []

    def tracking_print(*args, **kwargs) -> None:
        if args:
            message = str(args[0])
            if message.startswith(SCOPE_PREFIX):
                calls.append(message)

    monkeypatch.setattr(builtins, "print", tracking_print)
    return calls


def _build_schema_scope_rows_only(orm, Post, User, *, optimizer: bool = True):
    backend_name = orm._backend_name

    @orm.type(Post)
    class PostType:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, qs, info):
            print(f"{SCOPE_PREFIX}PostType.scope_rows", flush=True)
            return _published_posts_queryset(Post, backend_name, qs)

    @orm.type(User)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType]

    @strawberry.type
    class Query:
        @strawberry.field
        async def users(self, info: strawberry.types.Info) -> list[UserType]:
            return await orm.get_default_queryset(User)

    return orm.schema(query=Query, optimizer=optimizer)


def _build_schema_scope_rows_and_load(orm, Post, User):
    backend_name = orm._backend_name

    @orm.type(Post)
    class PostType:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, qs, info):
            print(f"{SCOPE_PREFIX}PostType.scope_rows", flush=True)
            return _published_posts_queryset(Post, backend_name, qs)

    def posts_load(qs, info):
        print(f"{SCOPE_PREFIX}UserType.posts.load", flush=True)
        return _posts_load_exclude_guide(Post, backend_name, qs)

    @orm.type(User)
    class UserType:
        id: auto
        name: auto
        posts: list[PostType] = orm.field.auto(scope=posts_load)

    @strawberry.type
    class Query:
        @strawberry.field
        async def users(self, info: strawberry.types.Info) -> list[UserType]:
            return await orm.get_default_queryset(User)

    return orm.schema(query=Query)


class TestScopingHookOrder:
    USERS_POSTS_QUERY = "{ users { name posts { title } } }"

    @pytest.mark.asyncio
    async def test_scope_rows_runs_during_optimizer_prefetch(
        self,
        monkeypatch,
        orm,
        seed,
        schema_execute_async,
        Post,
        User,
    ):
        calls = _install_print_tracker(monkeypatch)
        schema = _build_schema_scope_rows_only(orm, Post, User)
        result = await schema_execute_async(schema, self.USERS_POSTS_QUERY)
        assert result.errors is None
        messages = scope_messages(calls)
        assert messages.count(f"{SCOPE_PREFIX}PostType.scope_rows") >= 1
        assert f"{SCOPE_PREFIX}UserType.posts.load" not in messages

    @pytest.mark.asyncio
    async def test_scope_rows_runs_before_load_callable(
        self,
        monkeypatch,
        orm,
        seed,
        schema_execute_async,
        Post,
        User,
    ):
        calls = _install_print_tracker(monkeypatch)
        schema = _build_schema_scope_rows_and_load(orm, Post, User)
        result = await schema_execute_async(schema, self.USERS_POSTS_QUERY)
        assert result.errors is None
        messages = scope_messages(calls)
        assert_scope_rows_before_load(messages)
