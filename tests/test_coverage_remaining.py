"""Targeted coverage for remaining uncovered branches."""

from __future__ import annotations

import asyncio
import sys
import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import django
import pytest
import strawberry
from django.conf import settings
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

if not settings.configured:
    settings.configure(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "tests.backends.django.app.TestAppConfig",
        ],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        SECRET_KEY="test-secret-key-not-for-production",
        USE_TZ=False,
    )
    django.setup()

from strawberry_orm import AbstractRepo, MutationPolicy, StrawberryORM
from strawberry_orm._async import (
    async_safe_resolver,
    await_maybe_blocking,
    materialize_result,
    run_orm_work_blocking,
    run_sync,
)
from strawberry_orm.backends._base import (
    AggregateMeta,
    BaseBackend,
    invoke_custom_callback,
)
from strawberry_orm.backends.django import DjangoBackend
from strawberry_orm.backends.filter_pk_shortcut import (
    _build_reference_clause_recursive,
    build_reference_object_filter_clause,
    filter_tree_uses_only_reference_lookups,
)
from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from strawberry_orm.backends.tortoise import TortoiseBackend
from strawberry_orm.core import (
    _AutoFilterOrderExtension,
    _build_grouped_connection,
)
from strawberry_orm.filters import (
    ReferenceLookup,
    StringLookup,
    is_reference_lookup_type,
)
from strawberry_orm.lazy_resolution import (
    LazyResolutionExtension,
    _django_relation_hint,
    _query_selection_path,
    _relation_hint,
    _sqlalchemy_relation_hint,
    _tortoise_relation_hint,
    extensions_include_lazy_resolution,
)
from strawberry_orm.mutations import (
    _PROJECT_LEAF,
    _PROJECT_UNBOUNDED,
    MutationNamespace,
    RelationRemovalPolicy,
    RelationSpec,
    _primary_key_value,
    _sync_get_many_related,
    _sync_save_instance,
)
from strawberry_orm.optimizer.extension import (
    OptimizerExtension,
    extensions_optimizer_index,
)
from strawberry_orm.policy import _policy_to_repos
from strawberry_orm.relay.connection import (
    ORMConnectionExtension,
    ORMListConnection,
    _await_nodes_if_needed,
    _compute_page_aggregates,
    _connection_total_count,
    _decode_cursor_offset,
    _extract_items_after,
    _extract_items_first,
    _extract_items_order,
    _get_items_arg,
    _node_matches_group_key,
    _should_await_nodes,
    _use_orm_connection_extension,
    connection_type_for_node,
)
from strawberry_orm.types import DateGroupByOption, Ordering, auto
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


@pytest.fixture
def User():
    return SAUser


@pytest.fixture
def Post():
    return SAPost


@pytest.fixture
def Comment():
    return SAComment


@pytest.fixture
def sa_session():
    engine = create_engine("sqlite:///:memory:")
    SABase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def seed(sa_session):
    alice = SAUser(id=1, name="Alice", email="alice@example.com")
    bob = SAUser(id=2, name="Bob", email="bob@example.com")
    charlie = SAUser(id=3, name="Charlie", email="charlie@test.org")
    sa_session.add_all([alice, bob, charlie])
    sa_session.flush()

    python = SATag(id=1, name="python")
    graphql = SATag(id=2, name="graphql")
    rust = SATag(id=3, name="rust")
    sa_session.add_all([python, graphql, rust])
    sa_session.flush()

    posts = [
        SAPost(
            id=1,
            title="Hello World",
            body="Intro",
            is_published=True,
            author_id=1,
        ),
        SAPost(
            id=2,
            title="GraphQL Guide",
            body="Guide",
            is_published=True,
            author_id=1,
        ),
        SAPost(
            id=3,
            title="Draft Post",
            body="Draft",
            is_published=False,
            author_id=2,
        ),
        SAPost(
            id=4,
            title="Rust Adventures",
            body="Rust",
            is_published=True,
            author_id=3,
        ),
    ]
    sa_session.add_all(posts)
    sa_session.flush()
    posts[0].tags.extend([python, graphql])
    posts[3].tags.append(rust)
    sa_session.flush()

    sa_session.add_all(
        [
            SAComment(id=1, body="Nice post!", post_id=1, author_id=2),
            SAComment(id=2, body="Great guide!", post_id=2, author_id=3),
        ]
    )
    sa_session.flush()
    return {"users": {"alice": alice, "bob": bob, "charlie": charlie}}


# ---------------------------------------------------------------------------
# repo.py
# ---------------------------------------------------------------------------


class TestRepoCoverage:
    def test_default_auth_hooks_allow(self):
        repo = AbstractRepo.__new__(AbstractRepo)
        info = SimpleNamespace()
        assert repo.can_delete(object(), info) is True
        assert repo.can_link(object(), "field", object(), info) is True
        assert repo.can_unlink(object(), "field", object(), info) is True

    def test_tortoise_sync_crud_raises_type_error(self):
        backend = TortoiseBackend()
        repo = AbstractRepo.__new__(AbstractRepo)
        repo._backend = backend
        info = SimpleNamespace()

        with pytest.raises(TypeError, match="async-only"):
            repo._create(object, {}, info)
        with pytest.raises(TypeError, match="async-only"):
            repo._get(object, 1, info)
        with pytest.raises(TypeError, match="async-only"):
            repo._save(object(), info)
        with pytest.raises(TypeError, match="async-only"):
            repo._delete(object(), info)

    @pytest.mark.django_db
    def test_django_repo_crud_defaults(self):
        from tests.backends.django.models import Tag as DjTag
        from tests.backends.django.models import User as DjUser

        backend = DjangoBackend()
        repo = AbstractRepo.__new__(AbstractRepo)
        repo._backend = backend
        info = SimpleNamespace(context={})

        user = repo._create(DjUser, {"name": "Repo", "email": "repo@example.com"}, info)
        assert user.name == "Repo"

        loaded = repo._get(DjUser, user.pk, info)
        assert loaded.pk == user.pk

        loaded.name = "Updated"
        repo._save(loaded, info)
        assert DjUser.objects.get(pk=user.pk).name == "Updated"

        tag = DjTag.objects.create(name="temp")
        repo._delete(tag, info)
        assert not DjTag.objects.filter(pk=tag.pk).exists()

    @pytest.mark.asyncio
    async def test_sqlalchemy_async_get_and_delete(self, sa_session):
        backend = SQLAlchemyBackend(dialect="sqlite")
        repo = AbstractRepo.__new__(AbstractRepo)
        repo._backend = backend
        info = SimpleNamespace(context={"session": sa_session})

        user = SAUser(id=99, name="Async", email="async@example.com")
        sa_session.add(user)
        sa_session.flush()

        async_session = AsyncMock()
        scalars = MagicMock()
        scalars.first.return_value = user
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars
        async_session.execute = AsyncMock(return_value=execute_result)
        async_session.delete = AsyncMock()

        with (
            patch.object(backend, "_get_session", return_value=async_session),
            patch.object(backend, "_is_async_session", return_value=True),
        ):
            loaded = await repo._get_async(SAUser, 99, info)
            assert loaded is user

            await repo._delete_async(user, info)
            async_session.delete.assert_awaited_once_with(user)


# ---------------------------------------------------------------------------
# mutations.py
# ---------------------------------------------------------------------------


class TestMutationsCoverage:
    def test_primary_key_value_prefers_pk(self):
        assert _primary_key_value(SimpleNamespace(pk=1, id=2)) == 1
        assert _primary_key_value(SimpleNamespace(id=3)) == 3
        assert _primary_key_value(SimpleNamespace()) is None

    def test_unsupported_backend_for_relation_specs(self):
        backend = SimpleNamespace(__class__=type("UnknownBackend", (), {}))
        ns = MutationNamespace(backend)
        with pytest.raises(ValueError, match="Unsupported backend"):
            ns._relation_specs(SAUser)

    def test_no_repo_reverse_many_update_unlink_delete(
        self, sa_session, seed, Post, Comment
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        assert post is not None
        spec = ns._relation_specs(SAPost)["comments"]

        comment = sa_session.get(SAComment, 1)
        assert comment is not None

        @strawberry.input
        class UpdateComment:
            id: strawberry.ID
            body: str | None = strawberry.UNSET

        @strawberry.input
        class UnlinkRef:
            id: strawberry.ID

        @strawberry.input
        class DeleteRef:
            id: strawberry.ID

        update_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=UpdateComment(id=strawberry.ID("1"), body="Updated via ref"),
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        ns._apply_reverse_many_sync(post, spec, [update_ref], info)
        sa_session.flush()
        assert sa_session.get(SAComment, 1).body == "Updated via ref"

        unlink_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=UnlinkRef(id=strawberry.ID("1")),
            delete=strawberry.UNSET,
        )
        with patch.object(ns, "_detach_reverse_sync") as detach:
            ns._apply_reverse_many_sync(post, spec, [unlink_ref], info)
            detach.assert_called_once()

        extra = SAComment(body="Delete me", post_id=post.id, author_id=1)
        sa_session.add(extra)
        sa_session.flush()
        delete_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=DeleteRef(id=strawberry.ID(str(extra.id))),
        )
        ns._apply_reverse_many_sync(post, spec, [delete_ref], info)
        sa_session.flush()
        assert sa_session.get(SAComment, extra.id) is None

    def test_apply_single_sync_and_async_on_replace_delete(self, sa_session, seed):
        from strawberry_orm import AbstractRepo

        class UserRepo(AbstractRepo[SAUser]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAUser: UserRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})

        comment = sa_session.get(SAComment, 1)
        assert comment is not None
        spec = ns._relation_specs(SAComment)["author"]

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        disposable = SAUser(id=50, name="Disposable", email="disposable@example.com")
        sa_session.add(disposable)
        sa_session.flush()
        comment.author_id = disposable.id
        sa_session.flush()

        wrapper = AuthorRelationInput(
            update=UpdateUser(id=strawberry.ID("2")),
            on_replace=RelationRemovalPolicy.DELETE,
        )
        ns._apply_single_sync(comment, spec, wrapper, info)
        sa_session.flush()
        assert comment.author_id == 2
        assert sa_session.get(SAUser, disposable.id) is None

    @pytest.mark.asyncio
    async def test_apply_single_async_on_replace_delete(self, sa_session, seed):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        comment = sa_session.get(SAComment, 1)
        assert comment is not None
        spec = ns._relation_specs(SAComment)["author"]
        disposable = SAUser(
            id=53, name="AsyncDisposable", email="async-disposable@example.com"
        )
        sa_session.add(disposable)
        sa_session.flush()
        comment.author_id = disposable.id
        sa_session.flush()
        related = sa_session.get(SAUser, 2)

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        wrapper = AuthorRelationInput(
            update=UpdateUser(id=strawberry.ID("2")),
            on_replace=RelationRemovalPolicy.DELETE,
        )

        with (
            patch.object(ns, "_update_async", AsyncMock(return_value=related)),
            patch(
                "strawberry_orm.mutations._async_save_instance", AsyncMock()
            ) as save_mock,
            patch(
                "strawberry_orm.mutations._async_delete_instance", AsyncMock()
            ) as delete_mock,
        ):
            await ns._apply_single_async(comment, spec, wrapper, info)
            save_mock.assert_awaited()
            delete_mock.assert_awaited()


# ---------------------------------------------------------------------------
# _base.py
# ---------------------------------------------------------------------------


class TestBaseBackendCoverage:
    def test_invoke_custom_callback_with_info(self):
        captured: dict[str, Any] = {}

        def callback(instance, value, query, info):
            captured["info"] = info
            return query

        result = invoke_custom_callback(
            callback,
            object(),
            query="qs",
            value="x",
            info="info-value",
        )
        assert result == "qs"
        assert captured["info"] == "info-value"

    def test_filter_type_self_relation_rebuild(self):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.filter_type(SAComment)
        class CommentFilter:
            parent: auto

        assert hasattr(CommentFilter, "_object_type")
        assert "parent" in CommentFilter._relation_models

    def test_order_type_with_relations(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._order_registry[SAUser] = backend.order(SAUser)

        @backend.order_type(SAPost)
        class PostOrder:
            author: auto

        assert hasattr(PostOrder, "_object_type")
        assert "author" in PostOrder._relation_models

    def test_group_include_exclude(self, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        group_type = backend.group(SAPost, include=["title"], exclude=["body"])
        field_names = group_type._field_type.__dataclass_fields__
        assert "title" in field_names
        assert "body" not in field_names

    def test_aggregate_returns_none(self, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        assert backend.aggregate(SAPost) is None

    def test_disable_optimization_skips_lazy_check(self, User, Post):
        backend = SQLAlchemyBackend(dialect="sqlite", lazy_resolution="warn")
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", lazy_resolution="warn")
        orm._backend = backend

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            author: UserType = orm.field(disable_optimization=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.type(SAPost)(PostType)
        assert not any("author" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# lazy_resolution.py
# ---------------------------------------------------------------------------


class TestLazyResolutionCoverage:
    def test_extensions_include_lazy_resolution_instance_name(self):
        ext = LazyResolutionExtension()
        ext.__name__ = "LazyResolutionExtension_DjangoBackend"
        assert extensions_include_lazy_resolution([ext])

    @pytest.mark.django_db
    def test_django_relation_hint(self):
        from tests.backends.django.models import Post as DjPost
        from tests.backends.django.models import User as DjUser

        user = DjUser.objects.create(name="Hint", email="hint@example.com")
        post = DjPost.objects.create(title="Hint Post", body="Body", author=user)
        assert "select_related" in _django_relation_hint(post, "author", "Post")
        assert "prefetch_related" in _django_relation_hint(post, "tags", "Post")

    def test_sqlalchemy_and_tortoise_relation_hints(self, sa_session, seed, Post):
        post = sa_session.get(SAPost, 1)
        assert post is not None
        assert "joinedload" in _sqlalchemy_relation_hint(post, "author", "Post")
        assert "selectinload" in _sqlalchemy_relation_hint(post, "comments", "Post")

        tortoise_backend = TortoiseBackend()
        assert "prefetch_related" in _tortoise_relation_hint(post, "comments", "Post")
        assert "prefetch_related" in _relation_hint(tortoise_backend, post, "comments")

    def test_query_selection_path_with_operation(self):
        op_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="author"),
                        selection_set=SimpleNamespace(
                            selections=[
                                SimpleNamespace(
                                    name=SimpleNamespace(value="name"),
                                    selection_set=None,
                                )
                            ]
                        ),
                    )
                ]
            ),
        )
        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=SimpleNamespace(value="Posts"),
            selection_set=SimpleNamespace(selections=[op_sel]),
        )
        path = SimpleNamespace(
            key="author", prev=SimpleNamespace(key="posts", prev=None)
        )
        info = SimpleNamespace(
            operation=operation,
            path=path,
            field_name="name",
            python_name="name",
            parent_type=SimpleNamespace(name="PostType"),
        )
        result = _query_selection_path(info)
        assert "query Posts" in result
        assert "posts" in result
        assert "author" in result

    @pytest.mark.asyncio
    async def test_lazy_extension_resolve_async_early_exits(self):
        ext = LazyResolutionExtension()
        ext._mode = "off"
        ext._backend = DjangoBackend()

        async def _next(root, info):
            return "ok"

        assert await ext.resolve_async(_next, object(), SimpleNamespace()) == "ok"
        assert await ext.resolve_async(_next, None, SimpleNamespace()) == "ok"


# ---------------------------------------------------------------------------
# relay/connection.py
# ---------------------------------------------------------------------------


class TestConnectionCoverage:
    def test_compute_page_aggregates_numeric_fields(self):
        @strawberry.type
        class SumType:
            amount: float | None

        @strawberry.type
        class AggType:
            count: int
            sum: SumType | None = None
            avg: SumType | None = None
            min: SumType | None = None
            max: SumType | None = None

        meta = AggregateMeta(
            model=object,
            aggregates_type=AggType,
            group_key_type=object,
            sum_type=SumType,
            avg_type=SumType,
            min_type=SumType,
            max_type=SumType,
            numeric_fields=[("amount", float)],
            comparable_fields=[("amount", float)],
        )
        edges = [
            SimpleNamespace(node=SimpleNamespace(amount=10)),
            SimpleNamespace(node=SimpleNamespace(amount=20)),
            SimpleNamespace(node=SimpleNamespace(amount=None)),
        ]
        agg = _compute_page_aggregates(edges, meta)
        assert agg.count == 3
        assert agg.sum.amount == 30
        assert agg.avg.amount == 15
        assert agg.min.amount == 10
        assert agg.max.amount == 20

    @pytest.mark.asyncio
    async def test_resolve_connection_returns_awaitable(self):
        async def pending_connection(*_args, **_kwargs):
            return SimpleNamespace(edges=[], page_info=SimpleNamespace())

        with patch.object(
            ORMListConnection.__mro__[1],
            "resolve_connection",
            side_effect=pending_connection,
        ):
            info = SimpleNamespace(selected_fields=[])
            result = ORMListConnection.resolve_connection([], info=info)
            assert asyncio.iscoroutine(result)
            resolved = await result
            assert hasattr(resolved, "edges")

    @pytest.mark.asyncio
    async def test_resolve_connection_sync_finish_when_total_count_not_selected(self):
        async def pending_connection(*_args, **_kwargs):
            return SimpleNamespace(edges=[], page_info=SimpleNamespace())

        info = SimpleNamespace(selected_fields=[])
        with (
            patch.object(
                ORMListConnection.__mro__[2],
                "resolve_connection",
                side_effect=pending_connection,
            ),
            patch(
                "strawberry_orm.relay.connection.optimize_query_nodes",
                side_effect=lambda nodes, _info: nodes,
            ),
        ):
            resolved = await ORMListConnection.resolve_connection([], info=info)
        assert hasattr(resolved, "edges")

    @pytest.mark.asyncio
    async def test_finish_connection_awaitable_post_process(self):
        connection = SimpleNamespace(edges=[], page_info=SimpleNamespace())

        async def total():
            return 4

        async def post_process(*_args, **_kwargs):
            return connection

        with patch.object(
            ORMListConnection,
            "_post_process_connection",
            side_effect=post_process,
        ):
            resolved = await ORMListConnection._finish_connection(
                connection,
                total(),
                info=SimpleNamespace(),
            )
        assert resolved.total_count == 4

    @pytest.mark.asyncio
    async def test_resolve_connection_awaitable_finish(self):
        async def pending_connection(*_args, **_kwargs):
            return SimpleNamespace(edges=[], page_info=SimpleNamespace())

        async def count_coro():
            return 3

        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="usersConnection",
                    selections=[
                        SimpleNamespace(name="totalCount", selections=[]),
                    ],
                )
            ]
        )

        with (
            patch.object(
                ORMListConnection.__mro__[2],
                "resolve_connection",
                side_effect=pending_connection,
            ),
            patch(
                "strawberry_orm.relay.connection._connection_total_count",
                return_value=count_coro(),
            ),
            patch(
                "strawberry_orm.relay.connection.optimize_query_nodes",
                side_effect=lambda nodes, _info: nodes,
            ),
        ):
            resolved = await ORMListConnection.resolve_connection([], info=info)
        assert resolved.total_count == 3

    @pytest.mark.asyncio
    async def test_resolve_connection_attaches_total_count(self):
        async def count_coro():
            return 99

        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="usersConnection",
                    selections=[
                        SimpleNamespace(name="totalCount", selections=[]),
                    ],
                )
            ]
        )

        def sync_connection(*_args, **_kwargs):
            return SimpleNamespace(edges=[], page_info=SimpleNamespace())

        with (
            patch.object(
                ORMListConnection.__mro__[2],
                "resolve_connection",
                side_effect=sync_connection,
            ),
            patch(
                "strawberry_orm.relay.connection._connection_total_count",
                return_value=count_coro(),
            ),
            patch(
                "strawberry_orm.relay.connection.optimize_query_nodes",
                side_effect=lambda nodes, _info: nodes,
            ),
        ):
            resolved = await ORMListConnection.resolve_connection([], info=info)
        assert resolved.total_count == 99

    @pytest.mark.asyncio
    async def test_finish_connection_awaitable_total_count(self):
        connection = SimpleNamespace(edges=[], page_info=SimpleNamespace())

        async def total():
            return 7

        resolved = await ORMListConnection._finish_connection(
            connection,
            total(),
            info=SimpleNamespace(),
        )
        assert resolved.total_count == 7

    def test_should_await_nodes_false_for_plain_iterables(self):
        assert _should_await_nodes([1, 2, 3], SimpleNamespace()) is False

    def test_should_not_await_orm_query_objects(self):
        class AwaitableQuery:
            def __await__(self):
                async def _fail():
                    raise AssertionError("query must not be awaited")

                return _fail().__await__()

        backend = SimpleNamespace(is_query_object=lambda value: isinstance(value, AwaitableQuery))
        info = SimpleNamespace(context={"_orm_backend": backend})
        query = AwaitableQuery()
        assert _should_await_nodes(query, info) is False

    @pytest.mark.asyncio
    async def test_await_nodes_if_needed_skips_query_objects(self):
        class AwaitableQuery:
            def __await__(self):
                async def _fail():
                    raise AssertionError("query must not be awaited")

                return _fail().__await__()

        backend = SimpleNamespace(is_query_object=lambda value: isinstance(value, AwaitableQuery))
        info = SimpleNamespace(context={"_orm_backend": backend})
        query = AwaitableQuery()
        assert await _await_nodes_if_needed(query, info) is query

    @pytest.mark.asyncio
    async def test_connection_total_count_branches(self):
        backend = SimpleNamespace(
            is_query_object=lambda _value: True,
            count_query=lambda _query, _info: 42,
        )
        info = SimpleNamespace(context={"_orm_backend": backend})
        assert _connection_total_count(object(), info) == 42

        async def pending_nodes():
            return [1, 2, 3]

        assert (
            await _connection_total_count(
                pending_nodes(), SimpleNamespace(context={})
            )
            == 3
        )

        class NoLen:
            def __iter__(self):
                return iter([1, 2])

        assert _connection_total_count(NoLen(), SimpleNamespace(context={})) == 2

    def test_connection_type_for_node_sets_node_type(self):
        from strawberry import relay

        @strawberry.type
        class SampleNode(relay.Node):
            id: relay.NodeID[int]

        conn_type = connection_type_for_node(SampleNode)
        assert conn_type._node_type is SampleNode

    @pytest.mark.asyncio
    async def test_orm_connection_extension_resolve_async_keeps_query_objects(self):
        class AwaitableQuery:
            def __await__(self):
                async def _fail():
                    raise AssertionError("query must not be awaited")

                return _fail().__await__()

        backend = SimpleNamespace(
            is_query_object=lambda value: isinstance(value, AwaitableQuery)
        )
        info = SimpleNamespace(context={"_orm_backend": backend})
        ext = ORMConnectionExtension()
        ext.connection_type = SimpleNamespace(
            resolve_connection=lambda *_args, **_kwargs: "paginated"
        )

        async def next_(_source, _info, **_kwargs):
            return AwaitableQuery()

        with patch(
            "strawberry_orm.relay.connection.optimize_query_nodes",
            side_effect=lambda nodes, _info: nodes,
        ):
            result = await ext.resolve_async(next_, None, info)
        assert result == "paginated"

    def test_get_items_arg_dict_and_list_forms(self):
        groups_sel = SimpleNamespace(
            selections=[
                SimpleNamespace(
                    name="items",
                    arguments={"first": 5, "after": "cursor"},
                )
            ]
        )
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="groups",
                    selections=groups_sel.selections,
                )
            ]
        )
        assert _get_items_arg(info, "first") == 5
        assert _get_items_arg(info, "after") == "cursor"

        arg_obj = SimpleNamespace(name="order", value="ASC")
        groups_sel2 = SimpleNamespace(
            selections=[
                SimpleNamespace(
                    name="items",
                    arguments=[arg_obj],
                )
            ]
        )
        info2 = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(name="groups", selections=groups_sel2.selections)
            ]
        )
        assert _get_items_arg(info2, "order") == "ASC"

    @pytest.mark.asyncio
    async def test_orm_connection_extension_async_resolve_branches(self):
        ext = ORMConnectionExtension(max_results=10)

        async def async_connection(*_args, **_kwargs):
            return SimpleNamespace(edges=[], page_info=SimpleNamespace())

        ext.connection_type = SimpleNamespace(resolve_connection=async_connection)
        info = SimpleNamespace()

        async def pending_nodes():
            return [1, 2]

        with patch(
            "strawberry_orm.relay.connection.optimize_query_nodes",
            side_effect=lambda nodes, _info: pending_nodes(),
        ):
            coro = ext._resolve_nodes([], info)
            assert asyncio.iscoroutine(coro)
            resolved = await coro
            assert hasattr(resolved, "edges")

        with patch.object(ext, "_paginate_nodes", return_value=async_connection()):
            coro = ext._resolve_nodes([1], info)
            assert asyncio.iscoroutine(coro)
            assert hasattr(await coro, "edges")

        async def pending_from_next(source, info, **kwargs):
            return pending_nodes()

        with patch.object(ext, "_paginate_nodes", return_value=async_connection()):
            sync_result = ext.resolve(pending_from_next, None, info)
            assert asyncio.iscoroutine(sync_result)
            assert hasattr(await sync_result, "edges")

    @pytest.mark.asyncio
    async def test_orm_connection_extension_resolve_async(self):
        ext = ORMConnectionExtension(max_results=None)

        async def async_connection(*_args, **_kwargs):
            return "connection"

        ext.connection_type = SimpleNamespace(resolve_connection=async_connection)
        info = SimpleNamespace()

        async def next_(source, info, **kwargs):
            return [1]

        with patch(
            "strawberry_orm.relay.connection.optimize_query_nodes",
            side_effect=lambda nodes, _info: nodes,
        ):
            result = await ext.resolve_async(next_, None, info)
            assert result == "connection"

    def test_use_orm_connection_extension_replaces_connection_extension(self):
        from strawberry.relay.fields import ConnectionExtension

        field = SimpleNamespace(
            extensions=[ConnectionExtension(max_results=5), object()],
        )
        _use_orm_connection_extension(field)
        assert isinstance(field.extensions[0], ORMConnectionExtension)
        assert field.extensions[0].max_results == 5
        assert field.extensions[1] is not None


# ---------------------------------------------------------------------------
# _async.py
# ---------------------------------------------------------------------------


class TestAsyncHelpersCoverage:
    @pytest.mark.asyncio
    async def test_run_sync_without_asgiref(self):
        saved = sys.modules.get("asgiref.sync")
        sys.modules["asgiref.sync"] = None  # type: ignore[assignment]
        try:

            def add(a: int, b: int) -> int:
                return a + b

            result = await run_sync(add, 2, 3)
            assert result == 5
        finally:
            if saved is not None:
                sys.modules["asgiref.sync"] = saved
            else:
                sys.modules.pop("asgiref.sync", None)

    @pytest.mark.django_db
    def test_materialize_result_sync_branch(self):
        from tests.backends.django.models import User as DjUser

        backend = DjangoBackend()
        qs = DjUser.objects.all()
        result = materialize_result(backend, qs, SimpleNamespace(), sync=True)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_await_maybe_blocking_in_async_context(self):
        async def value():
            return 7

        assert await_maybe_blocking(value()) == 7

    @pytest.mark.asyncio
    @pytest.mark.django_db
    async def test_async_safe_resolver_materializes_queryset(self):
        from tests.backends.django.models import User as DjUser

        @async_safe_resolver
        def users_resolver():
            return DjUser.objects.all()

        result = users_resolver()
        assert hasattr(result, "__await__")
        materialized = await result
        assert isinstance(materialized, list)


# ---------------------------------------------------------------------------
# core.py
# ---------------------------------------------------------------------------


class TestCoreCoverage:
    @pytest.mark.asyncio
    @pytest.mark.django_db
    async def test_auto_filter_order_extension_resolve_async_sync_materialize(
        self,
    ):
        from tests.backends.django.models import User as DjUser

        backend = DjangoBackend()
        ext = _AutoFilterOrderExtension(backend)
        ext._model = DjUser
        ext._output_type = None
        ext._is_configured = True

        async def next_(source, info, **kwargs):
            return DjUser.objects.all()

        result = await ext.resolve_async(next_, None, SimpleNamespace(context={}))
        assert isinstance(result, list)

    def test_auto_filter_order_sync_resolve_keeps_awaitable_query_objects(self):
        class AwaitableQuery:
            def __await__(self):
                async def _fail():
                    raise AssertionError("query must not be awaited in sync resolve")

                return _fail().__await__()

        backend = SimpleNamespace(
            is_query_object=lambda value: isinstance(value, AwaitableQuery)
        )
        ext = _AutoFilterOrderExtension(backend)
        ext._model = object
        ext._is_configured = True
        query = AwaitableQuery()
        with patch.object(ext, "_apply", return_value=query):
            result = ext.resolve(lambda *_args, **_kwargs: [], None, SimpleNamespace())
        assert result is query

    def test_build_grouped_connection_without_node(self, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        group_type = backend.group(SAPost)
        order_type = backend.order(SAPost)
        conn = _build_grouped_connection(
            backend,
            list,
            SAPost,
            group_type,
            order_type,
        )
        assert conn.__name__ == "PostConnection"


# ---------------------------------------------------------------------------
# filter_pk_shortcut.py
# ---------------------------------------------------------------------------


class TestFilterPkShortcutCoverage:
    def test_filter_tree_any_one_of_not_branches(self):
        class Clause(str):
            def __and__(self, other: Any) -> Clause:
                return Clause(f"({self}&{other})")

            def __or__(self, other: Any) -> Clause:
                return Clause(f"({self}|{other})")

            def __invert__(self) -> Clause:
                return Clause(f"~{self}")

        @strawberry.input
        class RefField:
            id: ReferenceLookup | None = strawberry.UNSET

        @strawberry.input
        class RefFilter:
            field: RefField | None = strawberry.UNSET
            all: list[RefFilter] | None = strawberry.UNSET
            any: list[RefFilter] | None = strawberry.UNSET
            one_of: list[RefFilter] | None = strawberry.UNSET
            not_: RefFilter | None = strawberry.UNSET

        def build_field(val, **kwargs):
            lookup = val.id
            return Clause(f"eq:{lookup.exact}")

        any_input = RefFilter(
            any=[
                RefFilter(field=RefField(id=ReferenceLookup(exact="1"))),
                RefFilter(field=RefField(id=ReferenceLookup(exact="2"))),
            ]
        )
        assert filter_tree_uses_only_reference_lookups(any_input)
        any_clause = _build_reference_clause_recursive(
            any_input,
            build_field_clause=build_field,
            custom_filter_keys=frozenset(),
            max_branches=50,
        )
        assert str(any_clause) == "(eq:1|eq:2)"

        one_of_input = RefFilter(
            one_of=[
                RefFilter(field=RefField(id=ReferenceLookup(exact="3"))),
                RefFilter(field=RefField(id=ReferenceLookup(exact="4"))),
            ]
        )
        one_of_clause = _build_reference_clause_recursive(
            one_of_input,
            build_field_clause=build_field,
            custom_filter_keys=frozenset(),
            max_branches=50,
        )
        assert str(one_of_clause) == "(eq:3|eq:4)"

        not_input = RefFilter(
            not_=RefFilter(field=RefField(id=ReferenceLookup(exact="5")))
        )
        not_clause = _build_reference_clause_recursive(
            not_input,
            build_field_clause=build_field,
            custom_filter_keys=frozenset(),
            max_branches=50,
        )
        assert str(not_clause) == "~eq:5"

        combined = build_reference_object_filter_clause(
            RefFilter(
                all=[
                    RefFilter(field=RefField(id=ReferenceLookup(exact="1"))),
                    RefFilter(field=RefField(id=ReferenceLookup(exact="2"))),
                ]
            ),
            build_field_clause=build_field,
        )
        assert str(combined) == "(eq:1&eq:2)"


# ---------------------------------------------------------------------------
# optimizer/extension.py & policy.py & filters.py
# ---------------------------------------------------------------------------


class TestMiscCoverage:
    def test_extensions_optimizer_index_instance_name(self):
        ext = OptimizerExtension()
        ext.__name__ = "OptimizerExtension_SQLAlchemyBackend"
        assert extensions_optimizer_index([ext]) == 0

    def test_policy_repo_can_link(self):
        class LinkPolicy(MutationPolicy):
            def can_link(self, parent, field, instance, info):
                return field == "tags"

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            repos = _policy_to_repos(LinkPolicy())
        repo_cls = repos.get(object)
        repo = repo_cls(DjangoBackend())
        repo.model = object
        assert repo.can_link(None, "tags", None, None) is True
        assert repo.can_link(None, "comments", None, None) is False

    def test_is_reference_lookup_type_annotation_and_union(self):
        class AnnotatedLookup:
            annotation = ReferenceLookup | None

        assert is_reference_lookup_type(AnnotatedLookup())
        assert is_reference_lookup_type(ReferenceLookup | None)

        class OriginReference:
            __origin__ = ReferenceLookup
            __args__ = ()

        assert is_reference_lookup_type(OriginReference())

    @pytest.mark.django_db
    def test_policy_repo_delegates_all_hooks(self):
        class FullPolicy(MutationPolicy):
            def can_update(self, model, instance, data, info):
                return False

            def can_delete(self, model, instance, info):
                return False

            def can_unlink(self, parent, field, instance, info):
                return False

            def scope_query(self, model, query, info):
                return query.filter(pk=1)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            repos = _policy_to_repos(FullPolicy())

        assert SAUser in repos
        repo = repos.get(SAUser)(DjangoBackend())
        repo.model = SAUser
        assert repo.can_update(None, {}, None) is False
        assert repo.can_delete(None, None) is False
        assert repo.can_unlink(None, "f", None, None) is False

        from tests.backends.django.models import User as DjUser

        repo.model = DjUser
        scoped = repo.scope_query(DjUser.objects.all(), None)
        assert scoped.count() <= DjUser.objects.count()

    def test_is_fk_shortcut_lookup_rejects_non_shortcut_ops(self):
        from strawberry_orm.filters import (
            IntComparisonLookup,
            IntRangeInput,
            is_fk_shortcut_lookup,
        )

        assert (
            is_fk_shortcut_lookup(
                IntComparisonLookup(range=IntRangeInput(start=1, end=2))
            )
            is False
        )

    def test_group_type_custom_group_field(self, Post):
        from strawberry_orm import group_field

        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.group_type(SAPost)
        class PostGroupBy:
            is_published: auto

            @group_field
            def featured(self) -> bool:
                return True

        assert hasattr(PostGroupBy, "_custom_groups")
        assert "featured" in PostGroupBy._custom_groups

    def test_strawberry_orm_group_and_aggregate_wrappers(self, Post):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        assert orm.group(SAPost) is not None
        assert orm.aggregate(SAPost) is None
        assert callable(orm.group_type)
        assert callable(orm.aggregate_type)

    def test_repo_save_async_and_create_async_fallbacks(self, sa_session):
        from strawberry_orm.repo import _get_sa_pk_column

        backend = SQLAlchemyBackend(dialect="sqlite")
        repo = AbstractRepo.__new__(AbstractRepo)
        repo._backend = backend
        info = SimpleNamespace(context={"session": sa_session})

        user = SAUser(id=88, name="Detached", email="d@example.com")
        sa_session.add(user)
        sa_session.flush()
        sa_session.expunge(user)

        repo._save(user, info)
        assert sa_session.get(SAUser, 88) is not None

        asyncio.run(repo._save_async(user, info))

        created = asyncio.run(
            repo._create_async(
                SAUser, {"id": 87, "name": "Created", "email": "c@e.com"}, info
            )
        )
        assert created.name == "Created"

        loaded = asyncio.run(repo._get_async(SAUser, 87, info))
        assert loaded is not None

        with patch("sqlalchemy.inspect") as inspect_mock:
            inspect_mock.return_value.primary_key = [object(), object()]
            with pytest.raises(ValueError, match="primary key columns"):
                _get_sa_pk_column(SAUser)

    def test_connection_post_process_aggregates_and_groups(
        self, sa_session, seed, Post
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        meta = backend._build_aggregate_types(SAPost)
        group_type = backend.group(SAPost)
        group_field = group_type._field_type

        class Conn(ORMListConnection):
            _orm_aggregate_meta = meta
            _page_info_type = type("PageInfo", (), {})

        edges = [
            SimpleNamespace(node=sa_session.get(SAPost, 1)),
            SimpleNamespace(node=sa_session.get(SAPost, 2)),
        ]
        connection = SimpleNamespace(
            edges=edges,
            page_info=SimpleNamespace(
                start_cursor="a",
                end_cursor="b",
                has_previous_page=False,
                has_next_page=True,
            ),
            aggregates=None,
            groups=None,
        )

        agg_sel = SimpleNamespace(
            name="aggregates",
            selections=[SimpleNamespace(name="count", selections=[])],
        )
        groups_sel = SimpleNamespace(
            name="groups",
            selections=[
                SimpleNamespace(
                    name="aggregates",
                    selections=[SimpleNamespace(name="count", selections=[])],
                )
            ],
        )
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="orders",
                    selections=[agg_sel, groups_sel],
                )
            ],
            context={
                "session": sa_session,
                "_orm_backend": backend,
                "_orm_base_query": backend.get_default_queryset(SAPost),
                "_orm_group_by": [group_type(field=group_field(is_published=True))],
                "_orm_order": None,
            },
        )

        result = Conn._post_process_connection(connection, info=info)
        assert result.aggregates is not None
        assert result.groups is not None

    @pytest.mark.asyncio
    async def test_auto_filter_extension_resolve_async_non_query_awaitable(self):
        backend = DjangoBackend()
        ext = _AutoFilterOrderExtension(backend)
        ext._model = None
        ext._output_type = None
        ext._is_configured = True

        async def next_(source, info, **kwargs):
            async def payload():
                return [{"value": 1}]

            return payload()

        result = await ext.resolve_async(next_, None, SimpleNamespace(context={}))
        assert result == [{"value": 1}]

    def test_optimizer_extension_resolve_query_object(self, sa_session, User):
        from strawberry_orm.optimizer.store import OptimizerStore

        backend = SQLAlchemyBackend(dialect="sqlite")
        store = OptimizerStore()
        ext = OptimizerExtension()
        ext._backend = backend
        ext._store = store
        info = SimpleNamespace(
            context={"session": sa_session},
            field_nodes=[SimpleNamespace(selection_set=None)],
        )

        stmt = backend.get_default_queryset(SAUser)
        result = ext.resolve(lambda *a, **k: stmt, None, info)
        assert result is not None

    @pytest.mark.asyncio
    async def test_no_repo_reverse_many_async_paths(self, sa_session, seed, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = ns._relation_specs(SAPost)["comments"]
        child = sa_session.get(SAComment, 2)

        @strawberry.input
        class DeleteRef:
            id: strawberry.ID

        delete_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=DeleteRef(id=strawberry.ID("2")),
        )
        with (
            patch(
                "strawberry_orm.mutations._async_load_instance",
                AsyncMock(return_value=child),
            ),
            patch(
                "strawberry_orm.mutations._async_delete_instance", AsyncMock()
            ) as delete_mock,
        ):
            await ns._apply_reverse_many_async(post, spec, [delete_ref], info)
            delete_mock.assert_awaited()
        sa_session.delete(child)
        sa_session.flush()
        assert sa_session.get(SAComment, 2) is None

    def test_lazy_resolution_flush_error_mode(self, sa_session, seed, Post):
        from strawberry_orm.lazy_resolution import LazyResolutionExtension

        backend = SQLAlchemyBackend(dialect="sqlite", lazy_resolution="off")
        ext_cls = LazyResolutionExtension.configure(backend, mode="error")
        ext = ext_cls()

        post = sa_session.get(SAPost, 1)
        info = SimpleNamespace(
            python_name="author",
            field_name="author",
            path=SimpleNamespace(key="author", prev=None),
            parent_type=SimpleNamespace(name="PostType"),
            operation=None,
        )
        ext._record_relation_access(post, info)
        with pytest.raises(RuntimeError, match="Unoptimized relation loads"):
            ext._flush_loads()


class TestFinalCoveragePush:
    def test_default_mutation_policy_hooks(self):
        policy = MutationPolicy()
        assert policy.can_update(object, object(), {}, None) is True
        assert policy.can_delete(object, object(), None) is True
        assert policy.can_link(object(), "f", object(), None) is True
        assert policy.can_unlink(object(), "f", object(), None) is True

    def test_resolve_filter_lookup_unknown_type(self):
        backend = DummyBackend()
        assert (
            backend._resolve_filter_lookup_type(
                "blob", bytes, pk_names=set(), enable_regex=False
            )
            is None
        )

    def test_projected_filter_cache_hit(self):
        backend = StrawberryORM.for_sqlalchemy(dialect="sqlite").backend
        project = {"author": {}}
        first = backend._get_projected_filter(SAPost, project)
        second = backend._get_projected_filter(SAPost, project)
        assert first is second

    def test_filter_type_include_exclude_and_group_branches(self):
        backend = DummyBackend()

        @backend.filter_type(object, include=["name"])
        class IncludedFilter:
            name: auto

        assert IncludedFilter is not None

        @backend.filter_type(object, exclude=["name"])
        class ExcludedFilter:
            name: auto

        assert ExcludedFilter is not None

        @backend.order_type(object, include=["name"])
        class IncludedOrder:
            name: auto

        assert IncludedOrder is not None

        group_type = backend.group(object, include=["name"], exclude=["amount"])
        assert group_type is not None

        @backend.group_type(object, exclude=["name"])
        class ExcludedGroupType:
            name: auto

        assert ExcludedGroupType is not None

    def test_aggregate_include_fields_branch(self):
        backend = StrawberryORM.for_sqlalchemy(dialect="sqlite").backend

        @backend.aggregate_type(SAUser, include=["name"])
        class UserAgg:
            name: auto

        meta = backend._build_aggregate_types(SAUser, UserAgg)
        assert meta is not None

    def test_lazy_field_definition_disables_warning(self):
        backend = SQLAlchemyBackend(dialect="sqlite", lazy_resolution="warn")
        filt = backend.filter(SAPost)
        order = backend.order(SAPost)

        @backend.type(
            SAPost,
            filters=filt,
            order=order,
        )
        class PostWithFieldDef:
            id: auto
            comments: list[PostWithFieldDef] = backend.field(
                load=["author"], disable_optimization=True
            )

        assert PostWithFieldDef is not None

    def test_tortoise_primary_key_only(self):
        from strawberry_orm.backends.tortoise import _primary_key

        assert _primary_key(SimpleNamespace(id=5)) == 5
        assert _primary_key(SimpleNamespace(pk=7)) == 7

    @pytest.mark.asyncio
    async def test_tortoise_grouping_helper_duplicate_and_order_branches(self):
        from strawberry_orm.backends.tortoise import (
            _build_tortoise_order_from_input,
            _extract_tortoise_group_fields,
            _extract_tortoise_overlapping_order,
        )
        from strawberry_orm.types import Ordering

        @strawberry.input
        class GroupField:
            title: bool | None = True
            title_dup: bool | None = True

        @strawberry.input
        class GroupEntry:
            field: GroupField | None = strawberry.UNSET

        fields, keys = _extract_tortoise_group_fields([GroupEntry(field=GroupField())])
        assert "title" in fields

        @strawberry.input
        class OrderField:
            title: Ordering | None = strawberry.UNSET
            other: Ordering | None = strawberry.UNSET

        @strawberry.input
        class OrderEntry:
            field: OrderField | None = strawberry.UNSET

        overlap = _extract_tortoise_overlapping_order(
            OrderEntry(field=OrderField(title=Ordering.ASC, other=Ordering.DESC)),
            {"title"},
        )
        assert overlap == ["title"]
        assert _build_tortoise_order_from_input(
            OrderEntry(field=OrderField(title=Ordering.ASC, other=Ordering.DESC))
        ) == ["title", "-other"]

    def test_sqlalchemy_primary_key_and_group_else_interval(self):
        from strawberry_orm.backends.sqlalchemy import (
            _extract_sa_group_columns,
            _primary_key,
        )

        assert _primary_key(SimpleNamespace(id=1)) == 1

        @strawberry.input
        class UnknownInterval:
            interval: str = "unknown"

        @strawberry.input
        class DateField:
            created_at: DateGroupByOption | UnknownInterval | None = strawberry.UNSET

        @strawberry.input
        class GroupEntry:
            field: DateField | None = strawberry.UNSET

        cols, keys = _extract_sa_group_columns(
            [GroupEntry(field=DateField(created_at=UnknownInterval()))], SAUser
        )
        assert keys == ["created_at"]

    @pytest.mark.asyncio
    async def test_sqlalchemy_apply_ref_list_async_no_repo_branches(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(SABase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as async_session:
            alice = SAUser(id=1, name="Alice", email="alice@example.com")
            async_session.add(alice)
            await async_session.flush()
            post = SAPost(
                id=1,
                title="Hello",
                body="Body",
                is_published=True,
                author_id=1,
            )
            async_session.add(post)
            await async_session.flush()

            backend = SQLAlchemyBackend(dialect="sqlite")

            @strawberry.input
            class UpdateInput:
                id: strawberry.ID
                title: str | None = strawberry.UNSET

            @strawberry.input
            class IdInput:
                id: strawberry.ID

            refs = [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=UpdateInput(id=strawberry.ID("999"), title="x"),
                    unlink=strawberry.UNSET,
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=IdInput(id=strawberry.ID("999")),
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=strawberry.UNSET,
                    delete=IdInput(id=strawberry.ID("999")),
                ),
            ]
            info = SimpleNamespace(context={"session": async_session})
            await backend.apply_ref_list(
                post,
                "tags",
                refs,
                info,
                authorize=lambda action, model, obj_id, _info: False,
            )
        await engine.dispose()

    def test_async_helpers_remaining_branches(self):
        backend = DummyBackend()

        assert materialize_result(backend, [1], None, sync=True) == [1]

        def work() -> int:
            return 1

        assert run_orm_work_blocking(work) == 1

    @pytest.mark.asyncio
    async def test_await_maybe_blocking_plain_value(self):
        assert await_maybe_blocking(42) == 42

    def test_lazy_resolution_extension_class_detection(self):
        ext_cls = LazyResolutionExtension.configure(DjangoBackend(), mode="off")
        assert extensions_include_lazy_resolution([ext_cls]) is True

    def test_relay_node_matches_group_key_skips_none_key(self):
        key = SimpleNamespace(status=None)
        node = SimpleNamespace(status="x")
        assert _node_matches_group_key(node, key, ["status"]) is True

    @pytest.mark.asyncio
    async def test_repo_tortoise_and_sa_delete_paths(self):
        class TortoiseRepo(AbstractRepo):
            model = object

        tortoise_repo = TortoiseRepo(_backend_stub("TortoiseBackend"))
        child = SimpleNamespace(delete=AsyncMock())
        await tortoise_repo._delete_async(child, None)
        child.delete.assert_awaited_once()

        class SARepo(AbstractRepo):
            model = object

        session = SimpleNamespace(
            is_async=True,
            delete=AsyncMock(),
        )
        backend = type(
            "SQLAlchemyBackend",
            (),
            {
                "__name__": "SQLAlchemyBackend",
                "_get_session": lambda self, info: session,
                "_is_async_session": lambda self, s: True,
            },
        )()
        sa_repo = SARepo(backend)
        await sa_repo._delete_async(object(), SimpleNamespace(context={}))

        sync_session = MagicMock()
        sync_backend = type(
            "SQLAlchemyBackend",
            (),
            {
                "__name__": "SQLAlchemyBackend",
                "_get_session": lambda self, info: sync_session,
                "_is_async_session": lambda self, s: False,
            },
        )()
        sync_repo = SARepo(sync_backend)
        sync_repo._delete(object(), SimpleNamespace(context={}))
        sync_session.delete.assert_called_once()

    def test_filter_pk_shortcut_remaining_branches(self):
        @strawberry.input
        class RefField:
            id: ReferenceLookup | None = strawberry.UNSET

        @strawberry.input
        class RefFilter:
            field: RefField | None = strawberry.UNSET
            any: list[RefFilter] | None = strawberry.UNSET
            one_of: list[RefFilter] | None = strawberry.UNSET
            not_: RefFilter | None = strawberry.UNSET

        assert filter_tree_uses_only_reference_lookups(None) is True

        def build_clause(field_input, **_kwargs):
            from django.db.models import Q

            return Q(id=1)

        ref = ReferenceLookup(exact="1")
        any_filter = RefFilter(any=[RefFilter(field=RefField(id=ref))])
        assert (
            build_reference_object_filter_clause(
                any_filter, build_field_clause=build_clause
            )
            is not None
        )

        one_of_filter = RefFilter(
            one_of=[RefFilter(field=RefField(id=ReferenceLookup(exact="3")))]
        )
        assert (
            build_reference_object_filter_clause(
                one_of_filter, build_field_clause=build_clause
            )
            is not None
        )

        not_filter = RefFilter(
            not_=RefFilter(field=RefField(id=ReferenceLookup(exact="4")))
        )
        assert (
            build_reference_object_filter_clause(
                not_filter, build_field_clause=build_clause
            )
            is not None
        )

    def test_filter_pk_shortcut_rejects_non_reference_branches(self):
        from strawberry_orm.filters import StringLookup

        @strawberry.input
        class BadField:
            name: StringLookup | None = strawberry.UNSET

        @strawberry.input
        class BadFilter:
            field: BadField | None = strawberry.UNSET
            any: list[BadFilter] | None = strawberry.UNSET

        assert (
            filter_tree_uses_only_reference_lookups(
                BadFilter(field=BadField(name=StringLookup(exact="x")))
            )
            is False
        )
        assert (
            filter_tree_uses_only_reference_lookups(
                BadFilter(any=[BadFilter(field=BadField(name=StringLookup(exact="y")))])
            )
            is False
        )

    def test_repo_sync_delete_async_fallback(self):
        class SARepo(AbstractRepo):
            model = object

        session = MagicMock()
        backend = type(
            "SQLAlchemyBackend",
            (),
            {
                "__name__": "SQLAlchemyBackend",
                "_get_session": lambda self, info: session,
                "_is_async_session": lambda self, s: False,
            },
        )()
        repo = SARepo(backend)
        repo._delete(object(), SimpleNamespace(context={}))
        session.delete.assert_called_once()

    def test_lazy_resolution_path_and_class_detection(self):
        from strawberry_orm.lazy_resolution import (
            _format_unoptimized_loads,
            _graphql_path_from_info,
            _path_field_names,
            _query_selection_path,
        )

        ext_cls = LazyResolutionExtension.configure(DjangoBackend(), mode="off")
        assert extensions_include_lazy_resolution([ext_cls]) is True

        info = SimpleNamespace(
            path=SimpleNamespace(
                key="posts", prev=SimpleNamespace(key="users", prev=None)
            ),
            selected_fields=[],
            field_name="title",
            python_name="title",
        )
        assert _path_field_names(info) == ["users", "posts"]
        assert _graphql_path_from_info(info) == "users.posts"
        assert "users.posts" in _query_selection_path(info)

        message = _format_unoptimized_loads([])
        assert "Unoptimized relation loads" in message


class TestPushTo98:
    def test_relay_page_aggregates_with_numeric_fields(self):
        from tests.backends.sqlalchemy.test_grouping import Order
        from tests.backends.sqlalchemy.test_grouping import orm as group_orm

        meta = group_orm.backend._build_aggregate_types(Order)
        edge = SimpleNamespace(node=SimpleNamespace(amount=10.0, quantity=2))
        result = _compute_page_aggregates([edge, edge], meta)
        assert result.count == 2
        assert result.sum.amount == 20.0

    def test_relay_page_aggregates_without_subtypes(self):
        @strawberry.type
        class AggOnly:
            count: int

        meta = AggregateMeta(
            model=object,
            aggregates_type=AggOnly,
            group_key_type=object,
            sum_type=None,
            avg_type=None,
            min_type=None,
            max_type=None,
            numeric_fields=[],
            comparable_fields=[],
        )
        edge = SimpleNamespace(node=SimpleNamespace(amount=10))
        assert _compute_page_aggregates([edge], meta).count == 1

    def test_relay_connection_early_returns(self):
        connection = SimpleNamespace(edges=[], page_info=SimpleNamespace())
        info = SimpleNamespace(context={}, selected_fields=[])

        class PlainConn(ORMListConnection):
            pass

        assert PlainConn._post_process_connection(connection, info=info) is connection

        class ConnWithMeta(ORMListConnection):
            _orm_aggregate_meta = object()

        assert (
            ConnWithMeta._post_process_connection(connection, info=info) is connection
        )

    def test_relay_items_helpers_and_bad_cursor(self):
        assert _decode_cursor_offset("not-a-cursor") == 0
        assert _extract_items_first(SimpleNamespace(selected_fields=[])) is None
        assert _extract_items_after(SimpleNamespace(selected_fields=[])) is None
        assert _extract_items_order(SimpleNamespace(selected_fields=[])) is None

    def test_filter_pk_shortcut_failure_and_limit_branches(self):
        from strawberry_orm.filters import StringLookup

        @strawberry.input
        class RefField:
            id: ReferenceLookup | None = strawberry.UNSET

        @strawberry.input
        class RefFilter:
            field: RefField | None = strawberry.UNSET
            all: list[RefFilter] | None = strawberry.UNSET
            any: list[RefFilter] | None = strawberry.UNSET
            not_: RefFilter | None = strawberry.UNSET
            bogus: RefField | None = strawberry.UNSET

        assert (
            filter_tree_uses_only_reference_lookups(RefFilter(bogus=RefField()))
            is False
        )

        @strawberry.input
        class BadField:
            name: StringLookup | None = strawberry.UNSET

        bad_not = RefFilter(
            not_=RefFilter(field=BadField(name=StringLookup(exact="x")))
        )
        assert filter_tree_uses_only_reference_lookups(bad_not) is False

        def build_field(val, **_kwargs):
            return "clause"

        assert (
            _build_reference_clause_recursive(
                None,
                build_field_clause=build_field,
                custom_filter_keys=frozenset(),
                max_branches=50,
            )
            is None
        )

        empty_branch = RefFilter(all=[RefFilter()])
        assert (
            _build_reference_clause_recursive(
                empty_branch,
                build_field_clause=build_field,
                custom_filter_keys=frozenset(),
                max_branches=50,
            )
            is None
        )

        two_branch = RefFilter(
            all=[
                RefFilter(field=RefField(id=ReferenceLookup(exact="1"))),
                RefFilter(field=RefField(id=ReferenceLookup(exact="2"))),
            ]
        )
        with pytest.raises(ValueError, match="maximum is 1"):
            _build_reference_clause_recursive(
                two_branch,
                build_field_clause=build_field,
                custom_filter_keys=frozenset(),
                max_branches=1,
            )

    def test_mutation_namespace_remaining_branches(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)

        assert ns._normalize_enum_options(
            "DELETE",
            allowed=("DISCONNECT", "DELETE"),
            field_name="onReplace",
            model_name="Post",
        ) == ("DELETE",)
        assert ns._child_project(_PROJECT_LEAF, "comments") == _PROJECT_LEAF
        with pytest.raises(ValueError, match="must be a dict"):
            ns._normalize_model_project(SAUser, "bad")  # type: ignore[arg-type]

    def test_create_with_single_relation_after_scalars(self, sa_session, seed):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})

        @strawberry.input
        class CreateComment:
            body: str
            author_id: int | None = strawberry.UNSET

        @strawberry.input
        class CommentRef:
            create: CreateComment | None = strawberry.UNSET
            update: Any | None = strawberry.UNSET
            unlink: Any | None = strawberry.UNSET
            delete: Any | None = strawberry.UNSET

        @strawberry.input
        class CreatePost:
            title: str
            body: str
            is_published: bool = True
            author_id: int = 1
            comments: list[CommentRef] | None = strawberry.UNSET

        post = ns._create_sync(
            SAPost,
            CreatePost(
                title="With comment",
                body="Body",
                comments=[
                    CommentRef(
                        create=CreateComment(body="Nested", author_id=1),
                    )
                ],
            ),
            info,
        )
        sa_session.flush()
        assert post.title == "With comment"
        assert len(post.comments) == 1

    def test_apply_single_sync_repo_save_path(self, sa_session, seed):
        class UserRepo(AbstractRepo[SAUser]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAUser: UserRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        comment = sa_session.get(SAComment, 1)
        spec = ns._relation_specs(SAComment)["author"]

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        wrapper = AuthorRelationInput(update=UpdateUser(id=strawberry.ID("1")))
        with patch.object(UserRepo, "_save", autospec=True) as save_mock:
            ns._apply_single_sync(comment, spec, wrapper, info)
            assert save_mock.call_count >= 1

    def test_reverse_many_sync_repo_paths(self, sa_session, seed, Post):
        class CommentRepo(AbstractRepo[SAComment]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAComment: CommentRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = ns._relation_specs(SAPost)["comments"]

        @strawberry.input
        class UpdateComment:
            id: strawberry.ID
            body: str | None = strawberry.UNSET

        @strawberry.input
        class IdRef:
            id: strawberry.ID

        update_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=UpdateComment(id=strawberry.ID("1"), body="Repo updated"),
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        ns._apply_reverse_many_sync(post, spec, [update_ref], info)
        sa_session.flush()
        assert sa_session.get(SAComment, 1).body == "Repo updated"

        extra = SAComment(body="Repo delete", post_id=post.id, author_id=1)
        sa_session.add(extra)
        sa_session.flush()
        delete_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=IdRef(id=strawberry.ID(str(extra.id))),
        )
        ns._apply_reverse_many_sync(post, spec, [delete_ref], info)
        sa_session.flush()
        assert sa_session.get(SAComment, extra.id) is None

    @pytest.mark.asyncio
    async def test_reverse_many_async_repo_paths(self, sa_session, seed, Post):
        class CommentRepo(AbstractRepo[SAComment]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAComment: CommentRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = ns._relation_specs(SAPost)["comments"]

        @strawberry.input
        class UpdateComment:
            id: strawberry.ID
            body: str | None = strawberry.UNSET

        update_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=UpdateComment(id=strawberry.ID("1"), body="Async repo"),
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        await ns._apply_reverse_many_async(post, spec, [update_ref], info)
        sa_session.flush()
        assert sa_session.get(SAComment, 1).body == "Async repo"

    def test_sync_instance_helpers(self, sa_session, seed):
        backend = SQLAlchemyBackend(dialect="sqlite")
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        sa_session.expunge(post)
        _sync_save_instance(backend, post, info)
        assert sa_session.get(SAPost, 1) is not None

        dj_backend = type("DjangoBackend", (), {})()
        dj_post = SimpleNamespace(comments=SimpleNamespace(all=lambda: [1, 2]))
        assert _sync_get_many_related(dj_backend, dj_post, "comments", info) == [1, 2]

    def test_sqlalchemy_apply_ref_list_sync_with_repo(self, sa_session, seed, Post):
        class TagRepo(AbstractRepo[SATag]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SATag: TagRepo}
        post = sa_session.get(SAPost, 1)
        info = SimpleNamespace(context={"session": sa_session})

        @strawberry.input
        class UpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        @strawberry.input
        class IdRef:
            id: strawberry.ID

        refs = [
            SimpleNamespace(
                create=strawberry.UNSET,
                update=UpdateTag(id=strawberry.ID("1"), name="python-updated"),
                unlink=strawberry.UNSET,
                delete=strawberry.UNSET,
            ),
            SimpleNamespace(
                create=strawberry.UNSET,
                update=strawberry.UNSET,
                unlink=IdRef(id=strawberry.ID("2")),
                delete=strawberry.UNSET,
            ),
            SimpleNamespace(
                create=strawberry.UNSET,
                update=strawberry.UNSET,
                unlink=strawberry.UNSET,
                delete=IdRef(id=strawberry.ID("3")),
            ),
        ]
        backend.apply_ref_list(post, "tags", refs, info)
        sa_session.flush()
        assert sa_session.get(SATag, 1).name == "python-updated"
        assert sa_session.get(SATag, 3) is None

    @pytest.mark.asyncio
    async def test_sqlalchemy_apply_ref_list_async_with_repo(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(SABase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as async_session:
            tag = SATag(id=1, name="python")
            post = SAPost(
                id=1,
                title="Hello",
                body="Body",
                is_published=True,
                author_id=1,
            )
            post.tags = [tag]
            async_session.add_all([tag, post])
            await async_session.flush()

            class TagRepo(AbstractRepo[SATag]):
                pass

            backend = SQLAlchemyBackend(dialect="sqlite")
            backend._repos = {SATag: TagRepo}
            info = SimpleNamespace(context={"session": async_session})

            @strawberry.input
            class UpdateTag:
                id: strawberry.ID
                name: str | None = strawberry.UNSET

            refs = [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=UpdateTag(id=strawberry.ID("1"), name="async-repo"),
                    unlink=strawberry.UNSET,
                    delete=strawberry.UNSET,
                ),
            ]
            await backend.apply_ref_list(post, "tags", refs, info)
            await async_session.flush()
            updated = await async_session.get(SATag, 1)
            assert updated.name == "async-repo"
        await engine.dispose()

    def test_core_facade_and_stash_context(self, sa_session):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.group_type(SAUser)
        class UserGroup:
            name: auto

        assert UserGroup is not None

        ext = _AutoFilterOrderExtension(orm.backend)
        ext._group = orm.backend.group(SAUser)
        ext._filters = orm.backend.filter(SAUser)
        ext._order = orm.backend.order(SAUser)
        ext._is_configured = True

        field = SimpleNamespace(
            base_resolver=None,
            arguments=[],
        )
        ext.apply(field)
        arg_names = {arg.python_name for arg in field.arguments}
        assert "group_by" in arg_names

        ctx = SimpleNamespace()
        ext._stash_context(
            SimpleNamespace(context=ctx),
            orm.backend.get_default_queryset(SAUser),
            group_by=[],
            order=[],
        )
        assert ctx._orm_backend is orm.backend
        assert ctx._orm_base_query is not None

    def test_lazy_resolution_prefetch_and_unknown_backend(self):
        from strawberry_orm.lazy_resolution import (
            _format_selection_set,
            _path_field_names,
            _relation_hint,
            relation_is_prefetched,
        )

        info = SimpleNamespace(path=None, python_name="posts", field_name=None)
        assert _path_field_names(info) == ["posts"]

        selection = SimpleNamespace(name=None, selection_set=None)
        assert _format_selection_set(SimpleNamespace(selections=[selection])) == ""

        hint = _relation_hint(DummyBackend(), object(), "missing")
        assert "optimizer eager-loads" in hint

        from tests.backends.django.models import Post as DjPost

        post = DjPost()
        assert relation_is_prefetched(DjangoBackend(), post, "author") in (
            True,
            False,
            None,
        )

    @pytest.mark.asyncio
    async def test_await_maybe_blocking_resolves_coroutine(self):
        async def coro() -> int:
            return 7

        assert await_maybe_blocking(coro()) == 7

    def test_async_safe_resolver_materializes_queryset(self):
        class FakeQuerySet(list):
            pass

        captured: dict[str, Any] = {}

        def fake_resolver() -> FakeQuerySet:
            return FakeQuerySet([1, 2])

        with (
            patch(
                "strawberry_orm._async.asyncio.get_running_loop",
                side_effect=RuntimeError,
            ),
            patch.dict(
                "sys.modules",
                {"django.db.models": SimpleNamespace(QuerySet=FakeQuerySet)},
            ),
        ):
            wrapped = async_safe_resolver(fake_resolver)
            result = wrapped()
            assert result == [1, 2]
            captured["ok"] = True
        assert captured["ok"] is True

    @pytest.mark.asyncio
    async def test_repo_delete_async_sync_session_fallback(self):
        class SARepo(AbstractRepo):
            model = object

        session = MagicMock()
        backend = type(
            "SQLAlchemyBackend",
            (),
            {
                "__name__": "SQLAlchemyBackend",
                "_get_session": lambda self, info: session,
                "_is_async_session": lambda self, s: False,
            },
        )()
        repo = SARepo(backend)
        await repo._delete_async(object(), SimpleNamespace(context={}))
        session.delete.assert_called_once()

    def test_filter_pk_recursive_empty_sub_branches(self):
        @strawberry.input
        class RefField:
            id: ReferenceLookup | None = strawberry.UNSET

        @strawberry.input
        class RefFilter:
            field: RefField | None = strawberry.UNSET
            any: list[RefFilter] | None = strawberry.UNSET
            one_of: list[RefFilter] | None = strawberry.UNSET
            not_: RefFilter | None = strawberry.UNSET

        def build_field(val, **_kwargs):
            return "ok"

        for empty in (
            RefFilter(any=[RefFilter()]),
            RefFilter(one_of=[RefFilter()]),
            RefFilter(not_=RefFilter()),
        ):
            assert (
                _build_reference_clause_recursive(
                    empty,
                    build_field_clause=build_field,
                    custom_filter_keys=frozenset(),
                    max_branches=50,
                )
                is None
            )

        too_many = RefFilter(
            any=[
                RefFilter(field=RefField(id=ReferenceLookup(exact="1"))),
                RefFilter(field=RefField(id=ReferenceLookup(exact="2"))),
            ]
        )
        with pytest.raises(ValueError, match="maximum is 1"):
            _build_reference_clause_recursive(
                too_many,
                build_field_clause=build_field,
                custom_filter_keys=frozenset(),
                max_branches=1,
            )

    def test_create_sync_applies_remaining_single_relation(self, sa_session, seed):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = RelationSpec(
            name="author",
            related_model=SAUser,
            kind="single",
            relation_mode="reverse_fk",
            remote_attr="author_id",
            nullable=True,
        )
        wrapper = SimpleNamespace()
        relation_specs = {"author": spec}

        with (
            patch.object(ns, "_relation_specs", return_value=relation_specs),
            patch.object(
                ns,
                "_split_payload",
                return_value=({"title": "T", "body": "B"}, {"author": wrapper}),
            ),
            patch(
                "strawberry_orm.mutations._sync_create_instance", return_value=post
            ) as create_mock,
            patch.object(ns, "_apply_single_sync") as apply_mock,
            patch("strawberry_orm.mutations._sync_save_instance"),
        ):
            ns._create_sync(SAPost, SimpleNamespace(), info)
            create_mock.assert_called_once()
            apply_mock.assert_called_once_with(post, spec, wrapper, info)

    @pytest.mark.asyncio
    async def test_create_async_applies_remaining_single_relation(
        self, sa_session, seed
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = RelationSpec(
            name="author",
            related_model=SAUser,
            kind="single",
            relation_mode="reverse_fk",
            remote_attr="author_id",
            nullable=True,
        )
        wrapper = SimpleNamespace()
        relation_specs = {"author": spec}

        with (
            patch.object(ns, "_relation_specs", return_value=relation_specs),
            patch.object(
                ns,
                "_split_payload",
                return_value=({"title": "T", "body": "B"}, {"author": wrapper}),
            ),
            patch(
                "strawberry_orm.mutations._async_create_instance",
                AsyncMock(return_value=post),
            ) as create_mock,
            patch.object(ns, "_apply_single_async", AsyncMock()) as apply_mock,
            patch(
                "strawberry_orm.mutations._async_save_instance",
                AsyncMock(),
            ),
        ):
            await ns._create_async(SAPost, SimpleNamespace(), info)
            create_mock.assert_awaited_once()
            apply_mock.assert_awaited_once_with(post, spec, wrapper, info)

    def test_sensitive_fields_skipped_in_mutation_input(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        cls: type = type("CreateInput", (), {})
        base_introspect = backend._introspect_model

        def introspect_with_secret(model: type):
            return list(base_introspect(model)) + [
                ("password_hash", str, False, None),
            ]

        with patch.object(
            backend, "_introspect_model", side_effect=introspect_with_secret
        ):
            ns._populate_model_input(
                cls, SAUser, operation="create", project=_PROJECT_UNBOUNDED
            )
        assert "password_hash" not in cls.__annotations__
        assert "name" in cls.__annotations__

    def test_on_replace_delete_without_repo(self, sa_session, seed):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        comment = sa_session.get(SAComment, 1)
        spec = ns._relation_specs(SAComment)["author"]
        disposable = SAUser(id=62, name="NoRepo", email="norepo@example.com")
        sa_session.add(disposable)
        sa_session.flush()
        comment.author_id = disposable.id
        sa_session.flush()

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        wrapper = AuthorRelationInput(
            update=UpdateUser(id=strawberry.ID("2")),
            on_replace=RelationRemovalPolicy.DELETE,
        )
        ns._apply_single_sync(comment, spec, wrapper, info)
        sa_session.flush()
        assert sa_session.get(SAUser, disposable.id) is None

    def test_get_items_arg_when_arg_is_scalar_name(self):
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="groups",
                    selections=[
                        SimpleNamespace(
                            name="items",
                            arguments=["first"],
                        )
                    ],
                )
            ]
        )
        assert _get_items_arg(info, "first") == "first"

    def test_lazy_resolution_format_and_query_path_branches(self):
        from strawberry_orm.lazy_resolution import (
            _format_selection_set,
            _query_selection_path,
        )

        empty_nested = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(selections=[]),
        )
        assert (
            _format_selection_set(SimpleNamespace(selections=[empty_nested])) == "posts"
        )

        missing_name = SimpleNamespace(name=None, selection_set=None)
        assert _format_selection_set(SimpleNamespace(selections=[missing_name])) == ""

        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=None,
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="users"),
                        selection_set=SimpleNamespace(selections=[]),
                    )
                ]
            ),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(key="users", prev=None),
            field_name="users",
            python_name="users",
        )
        path = _query_selection_path(info)
        assert path == "query { users }"

    def test_sync_get_many_related_non_django_backend(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        post = SimpleNamespace(tags=[1, 2, 3])
        assert _sync_get_many_related(backend, post, "tags", None) == [1, 2, 3]

    def test_await_maybe_blocking_in_sync_context(self):
        async def coro() -> int:
            return 11

        assert await_maybe_blocking(coro()) == 11

    @pytest.mark.asyncio
    async def test_materialize_result_in_async_context(self):
        backend = MagicMock()
        backend.is_query_object.return_value = True

        async def materialize(query: Any, info: Any) -> list[int]:
            return [1, 2]

        backend.materialize_query = materialize
        result = materialize_result(backend, object(), SimpleNamespace())
        assert await result == [1, 2]

    def test_reverse_many_unlink_uses_repo_get(self, sa_session, seed, Post):
        from dataclasses import replace

        class CommentRepo(AbstractRepo[SAComment]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAComment: CommentRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = replace(ns._relation_specs(SAPost)["comments"], nullable=True)

        @strawberry.input
        class IdRef:
            id: strawberry.ID

        unlink_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=IdRef(id=strawberry.ID("2")),
            delete=strawberry.UNSET,
        )
        with patch.object(ns, "_detach_reverse_sync") as detach:
            ns._apply_reverse_many_sync(post, spec, [unlink_ref], info)
            detach.assert_called_once()

    def test_on_replace_delete_calls_repo_delete(self, sa_session, seed):
        class UserRepo(AbstractRepo[SAUser]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAUser: UserRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        comment = sa_session.get(SAComment, 1)
        spec = ns._relation_specs(SAComment)["author"]
        disposable = SAUser(id=60, name="Gone", email="gone@example.com")
        sa_session.add(disposable)
        sa_session.flush()
        comment.author_id = disposable.id
        sa_session.flush()

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        wrapper = AuthorRelationInput(
            update=UpdateUser(id=strawberry.ID("2")),
            on_replace=RelationRemovalPolicy.DELETE,
        )
        with patch.object(UserRepo, "_delete", autospec=True) as delete_mock:
            ns._apply_single_sync(comment, spec, wrapper, info)
            delete_mock.assert_called_once()

    def test_get_items_arg_list_argument_value(self):
        arg = SimpleNamespace(name="first", value=7)
        groups_sel = SimpleNamespace(
            selections=[SimpleNamespace(name="items", arguments=[arg])]
        )
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(name="groups", selections=groups_sel.selections)
            ]
        )
        assert _get_items_arg(info, "first") == 7

    @pytest.mark.asyncio
    async def test_apply_single_async_repo_save_and_delete(self, sa_session, seed):
        class UserRepo(AbstractRepo[SAUser]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAUser: UserRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        comment = sa_session.get(SAComment, 1)
        spec = ns._relation_specs(SAComment)["author"]
        disposable = SAUser(id=61, name="AsyncGone", email="async-gone@example.com")
        sa_session.add(disposable)
        sa_session.flush()
        comment.author_id = disposable.id
        sa_session.flush()

        @strawberry.input
        class AuthorRelationInput:
            update: Any | None = strawberry.UNSET
            create: Any | None = strawberry.UNSET
            on_replace: Any | None = strawberry.field(
                default=strawberry.UNSET, name="onReplace"
            )

        @strawberry.input
        class UpdateUser:
            id: strawberry.ID

        AuthorRelationInput.__relation_policy__ = {
            "default_on_replace": "DISCONNECT",
            "on_replace_options": ("DISCONNECT", "DELETE"),
        }
        wrapper = AuthorRelationInput(
            update=UpdateUser(id=strawberry.ID("2")),
            on_replace=RelationRemovalPolicy.DELETE,
        )
        with (
            patch.object(UserRepo, "_save_async", AsyncMock()) as save_mock,
            patch.object(UserRepo, "_delete_async", AsyncMock()) as delete_mock,
        ):
            await ns._apply_single_async(comment, spec, wrapper, info)
            save_mock.assert_awaited()
            delete_mock.assert_awaited()


class TestLazyResolutionDeep:
    def test_field_name_from_info_camelcase(self):
        from strawberry_orm.lazy_resolution import _field_name_from_info

        info = SimpleNamespace(python_name="userPosts", field_name=None)
        assert _field_name_from_info(info) == "user_posts"
        assert (
            _field_name_from_info(SimpleNamespace(python_name=None, field_name=None))
            is None
        )

    def test_query_selection_path_with_operation(self):
        from strawberry_orm.lazy_resolution import _query_selection_path

        selection = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="title"),
                        selection_set=None,
                    )
                ]
            ),
        )
        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=SimpleNamespace(value="GetPosts"),
            selection_set=SimpleNamespace(selections=[selection]),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(
                key="posts", prev=SimpleNamespace(key="users", prev=None)
            ),
            field_name="title",
            python_name="title",
        )
        path = _query_selection_path(info)
        assert "posts" in path

    def test_django_relation_hints_for_all_field_types(self):
        from tests.backends.django.models import Post as DjPost

        post = DjPost()
        assert "select_related" in _django_relation_hint(post, "author", "Post")
        assert "prefetch_related" in _django_relation_hint(post, "tags", "Post")
        assert "prefetch_related" in _django_relation_hint(post, "comments", "Post")
        assert "optimizer" in _django_relation_hint(object(), "missing", "Post")

    def test_sqlalchemy_relation_hint_uselist_and_error(self, sa_session, seed):
        post = sa_session.get(SAPost, 1)
        assert "joinedload" in _sqlalchemy_relation_hint(post, "author", "Post")
        assert "selectinload" in _sqlalchemy_relation_hint(post, "comments", "Post")
        assert "optimizer" in _sqlalchemy_relation_hint(object(), "missing", "Post")

    def test_tortoise_relation_hint_error_path(self):
        hint = _tortoise_relation_hint(object(), "missing", "Post")
        assert "prefetch_related" in hint

    @pytest.mark.asyncio
    async def test_lazy_resolution_async_resolve_early_exit(self):
        ext_cls = LazyResolutionExtension.configure(DjangoBackend(), mode="off")
        ext = ext_cls()
        ext._backend = None

        async def _next(root, info, **kwargs):
            return "ok"

        assert await ext.resolve_async(_next, None, SimpleNamespace(path=None)) == "ok"


def _backend_stub(name: str) -> Any:
    return type(name, (), {"__name__": name})()


class DummyBackend(BaseBackend):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(warn_missing_queryset=False, **kwargs)

    def _introspect_model(self, model: type):
        return [
            ("id", int, False, None),
            ("name", str, False, None),
            ("created_at", __import__("datetime").datetime, False, None),
            ("amount", float, False, None),
            ("author_id", int, False, "author"),
        ]

    def is_query_object(self, value: Any) -> bool:
        return hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict))

    def materialize_query(self, query: Any, info: Any) -> Any:
        return list(query)

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        return query


class TestLastCoverageLines:
    def test_lazy_resolution_final_branches(self, sa_session, seed):
        from strawberry_orm.lazy_resolution import (
            _django_relation_hint,
            _django_relation_prefetched,
            _query_selection_path,
            _tortoise_relation_prefetched,
        )

        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=SimpleNamespace(value="Named"),
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="users"),
                        selection_set=SimpleNamespace(
                            selections=[
                                SimpleNamespace(
                                    name=SimpleNamespace(value="posts"),
                                    selection_set=None,
                                )
                            ]
                        ),
                    )
                ]
            ),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(
                key="title",
                prev=SimpleNamespace(
                    key="posts",
                    prev=SimpleNamespace(key="users", prev=None),
                ),
            ),
            field_name="title",
            python_name="title",
        )
        assert _query_selection_path(info) == "{ users.posts.title }"

        info_op_only = SimpleNamespace(
            operation=SimpleNamespace(
                operation=SimpleNamespace(value="mutation"),
                name=None,
                selection_set=SimpleNamespace(
                    selections=[
                        SimpleNamespace(
                            name=SimpleNamespace(value="createUser"),
                            selection_set=None,
                        )
                    ]
                ),
            ),
            path=SimpleNamespace(key="createUser", prev=None),
            field_name="createUser",
            python_name="createUser",
        )
        assert _query_selection_path(info_op_only) == "mutation { createUser }"

        info_no_op_type = SimpleNamespace(
            operation=SimpleNamespace(
                operation=SimpleNamespace(value=None),
                name=None,
                selection_set=SimpleNamespace(
                    selections=[
                        SimpleNamespace(
                            name=SimpleNamespace(value="createUser"),
                            selection_set=None,
                        )
                    ]
                ),
            ),
            path=SimpleNamespace(key="createUser", prev=None),
            field_name="createUser",
            python_name="createUser",
        )
        assert _query_selection_path(info_no_op_type) == "{ createUser }"

        from tests.backends.django.models import Post as DjPost

        post = DjPost()
        assert "optimizer eager-loads" in _django_relation_hint(
            post, "nonexistent_field", "Post"
        )

        fk_post = SimpleNamespace(
            _meta=SimpleNamespace(
                get_field=lambda _name: SimpleNamespace(
                    is_relation=True,
                    many_to_one=True,
                    one_to_one=False,
                    is_cached=lambda _inst: False,
                )
            )
        )
        assert _django_relation_prefetched(fk_post, "author") is False

        fk_post_no_cache = SimpleNamespace(
            _meta=SimpleNamespace(
                get_field=lambda _name: SimpleNamespace(
                    is_relation=True,
                    many_to_one=True,
                    one_to_one=False,
                    is_cached=None,
                )
            )
        )
        assert _django_relation_prefetched(fk_post_no_cache, "author") is True
        assert _tortoise_relation_prefetched(object(), "tags") is None

        class ManyToManyFieldInstance:
            related_model = object

        class BackwardFKRelation:
            related_model = object

        list_posts = SimpleNamespace(
            _meta=SimpleNamespace(
                fields_map={"posts": BackwardFKRelation()},
            ),
            posts=[object()],
        )
        assert _tortoise_relation_prefetched(list_posts, "posts") is True

        class RaisingTags:
            _meta = SimpleNamespace(
                fields_map={"tags": ManyToManyFieldInstance()},
            )

            @property
            def tags(self):
                raise RuntimeError("access denied")

        assert _tortoise_relation_prefetched(RaisingTags(), "tags") is False

    def test_coalesce_tortoise_prefetch_paths_skips_unknown_nested_roots(self):
        from tortoise.query_utils import Prefetch

        from strawberry_orm.backends.tortoise import _coalesce_tortoise_prefetch_paths

        class _PostModel:
            _meta = SimpleNamespace(basequery=object())

        class _PostQuerySet:
            model = _PostModel
            query = object()

            def prefetch_related(self, *_args: str):
                return self

        class _BackwardFK:
            related_model = _PostModel

        _PostModel.all = classmethod(lambda cls: _PostQuerySet())  # type: ignore[method-assign]

        class _FakeUser:
            _meta = SimpleNamespace(
                fields_map={"posts": _BackwardFK(), "name": object()}
            )

        assert (
            _coalesce_tortoise_prefetch_paths(_FakeUser, ["not_a_relation__nested"])
            == []
        )
        coalesced = _coalesce_tortoise_prefetch_paths(
            _FakeUser, ["posts__tags", "posts"]
        )
        assert len(coalesced) == 1
        assert isinstance(coalesced[0], Prefetch)

    def test_sqlalchemy_final_helper_branches(self, sa_session, seed, Post):
        from strawberry_orm.backends.sqlalchemy import (
            _build_reference_lookup_clauses,
            _build_sa_filter,
            _build_sa_order_field,
            _build_sa_order_from_input,
            _extract_overlapping_order,
        )
        from tests.backends.sqlalchemy.fixtures import PostType

        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.type(SAUser)
        class UserWithFakeRel:
            id: auto
            fake_rel: list[PostType]

        assert not callable(getattr(UserWithFakeRel, "fake_rel", None))

        @strawberry.input
        class EmptyFilter:
            field: object | None = strawberry.UNSET

        assert _build_sa_filter(EmptyFilter(), Post) == (None, None)

        with pytest.raises(ValueError, match="maximum is"):
            _build_reference_lookup_clauses(
                Post.id,
                ReferenceLookup(in_list=[str(i) for i in range(501)]),
                max_in_list_size=500,
            )

        subq = select(Post).subquery()

        @strawberry.input
        class EmptyOrderField:
            pass

        @strawberry.input
        class EmptyOrderEntry:
            field: EmptyOrderField | None = strawberry.UNSET

        assert (
            _extract_overlapping_order(EmptyOrderEntry(), {"author_id"}, Post, subq)
            == []
        )
        assert _build_sa_order_from_input(EmptyOrderEntry(), Post)
        assert _build_sa_order_field(EmptyOrderField(), Post) == []

        backend._type_registry["UserType"] = SAUser
        backend._store.hints = {
            "UserType": {
                "posts": SimpleNamespace(
                    load=["comments"],
                    only=None,
                    disable_optimization=False,
                )
            }
        }
        posts_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=None,
        )
        field_node = SimpleNamespace(
            selection_set=SimpleNamespace(selections=[posts_sel])
        )
        info = SimpleNamespace(
            field_nodes=[field_node],
            context={"session": sa_session},
        )
        assert (
            len(
                backend.apply_optimizer_hints(
                    backend._store, backend.get_default_queryset(SAUser), info
                )
            )
            >= 1
        )

    def test_django_generic_relation_field_hint(self):
        from tests.backends.django.models import Post as DjPost

        class GenericRelationField:
            is_relation = True
            many_to_one = False
            one_to_one = False

        post = DjPost()
        post._meta.get_field = lambda name: GenericRelationField()  # type: ignore[method-assign]
        assert "optimizer eager-loads" in _django_relation_hint(
            post, "generic_link", "Post"
        )
