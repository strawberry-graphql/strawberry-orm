"""Tests targeting the final uncovered lines for 100% coverage."""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import django
import pytest
import strawberry
from django.conf import settings
from sqlalchemy import Boolean, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

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

from strawberry_orm import AbstractRepo, Ordering
from strawberry_orm._async import async_safe_resolver
from strawberry_orm.backends._base import AggregateMeta, BaseBackend
from strawberry_orm.backends.django import (
    DjangoBackend,
    _extract_django_overlapping_order,
)
from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _build_sa_filter,
    _coerce_reference_value,
    _extract_overlapping_order,
    _extract_sa_group_columns,
    _introspect_sa_model,
    _invoke_aggregate_handler,
)
from strawberry_orm.filters import StringLookup
from strawberry_orm.lazy_resolution import (
    LazyResolutionExtension,
    _django_relation_hint,
    _django_relation_prefetched,
    _format_selection_set,
    _parent_graphql_type,
    _query_selection_path,
    _sqlalchemy_relation_prefetched,
    _tortoise_relation_prefetched,
    relation_is_prefetched,
)
from strawberry_orm.mutations import (
    MutationNamespace,
    _async_get_many_related,
)
from strawberry_orm.relay.connection import _get_items_arg
from strawberry_orm.types import (
    FieldDefinition,
    auto,
)
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


@pytest.fixture
def Post():
    return SAPost


@pytest.fixture
def User():
    return SAUser


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
    sa_session.add_all([alice, bob])
    sa_session.flush()
    python = SATag(id=1, name="python")
    sa_session.add(python)
    sa_session.flush()
    post = SAPost(id=1, title="Hello", body="Body", is_published=True, author_id=1)
    post2 = SAPost(id=2, title="Other", body="Body", is_published=True, author_id=1)
    sa_session.add_all([post, post2])
    sa_session.flush()
    sa_session.add_all(
        [
            SAComment(id=1, body="c1", post_id=1, author_id=2),
            SAComment(id=2, body="c2", post_id=1, author_id=2),
        ]
    )
    sa_session.flush()
    return {"users": {"alice": alice, "bob": bob}}


class _CovBase(DeclarativeBase):
    pass


class _CovUser(_CovBase):
    __tablename__ = "cov_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    posts: Mapped[list[_CovPost]] = relationship(back_populates="author")


class _CovPost(_CovBase):
    __tablename__ = "cov_post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("cov_user.id"), nullable=True
    )
    author: Mapped[_CovUser | None] = relationship(back_populates="posts")


class _CovImplCol(_CovBase):
    __tablename__ = "cov_impl"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flag: Mapped[bool] = mapped_column(Boolean)


@pytest.fixture
def cov_session():
    engine = create_engine("sqlite:///:memory:")
    _CovBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


class TestAsyncRemaining:
    @pytest.mark.asyncio
    async def test_async_safe_resolver_passthrough_coroutine(self):
        @async_safe_resolver
        async def coro_resolver() -> str:
            return "ok"

        assert coro_resolver is not await coro_resolver()

    def test_async_safe_resolver_without_django_queryset_import(self):
        class FakeQS(list):
            pass

        def resolver() -> FakeQS:
            return FakeQS([1])

        with (
            patch(
                "strawberry_orm._async.asyncio.get_running_loop",
                side_effect=RuntimeError,
            ),
            patch.dict("sys.modules", {"django.db.models": None}),
        ):
            wrapped = async_safe_resolver(resolver)
            assert wrapped() == [1]


class TestMutationsRemaining:
    def test_reverse_many_delete_removes_from_sqlalchemy_list(
        self, sa_session, seed, Post
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        post = sa_session.get(SAPost, 1)
        spec = ns._relation_specs(SAPost)["comments"]
        child = sa_session.get(SAComment, 2)
        assert child in post.comments

        @strawberry.input
        class DeleteRef:
            id: strawberry.ID

        delete_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=DeleteRef(id=strawberry.ID("2")),
        )
        ns._apply_reverse_many_sync(post, spec, [delete_ref], info)
        sa_session.flush()
        assert child not in post.comments

    def test_detach_reverse_sync_with_repo(self, sa_session, seed):
        from dataclasses import replace

        class CommentRepo(AbstractRepo[SAComment]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAComment: CommentRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        child = sa_session.get(SAComment, 1)
        spec = replace(ns._relation_specs(SAPost)["comments"], nullable=True)

        with patch.object(CommentRepo, "_save") as save_mock:
            ns._detach_reverse_sync(child, spec, info)
            save_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_reverse_many_async_repo_unlink_and_delete(
        self, sa_session, seed, Post
    ):
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
        with patch.object(ns, "_detach_reverse_async", AsyncMock()) as detach:
            await ns._apply_reverse_many_async(post, spec, [unlink_ref], info)
            detach.assert_awaited()

        extra = SAComment(body="gone", post_id=post.id, author_id=1)
        sa_session.add(extra)
        sa_session.flush()
        delete_ref = SimpleNamespace(
            create=strawberry.UNSET,
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=IdRef(id=strawberry.ID(str(extra.id))),
        )
        with patch.object(CommentRepo, "_delete_async", AsyncMock()) as delete_mock:
            await ns._apply_reverse_many_async(post, spec, [delete_ref], info)
            delete_mock.assert_awaited()

    @pytest.mark.asyncio
    async def test_detach_reverse_async_repo_save(self, sa_session, seed):
        from dataclasses import replace

        class CommentRepo(AbstractRepo[SAComment]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._repos = {SAComment: CommentRepo}
        ns = MutationNamespace(backend)
        info = SimpleNamespace(context={"session": sa_session})
        child = sa_session.get(SAComment, 1)
        spec = replace(ns._relation_specs(SAPost)["comments"], nullable=True)

        with patch.object(CommentRepo, "_save_async", AsyncMock()) as save_mock:
            await ns._detach_reverse_async(child, spec, info)
            save_mock.assert_awaited()

    @pytest.mark.asyncio
    async def test_async_get_many_related(self):
        class Manager:
            async def all(self) -> list[int]:
                return [1, 2, 3]

        instance = SimpleNamespace(tags=Manager())
        result = await _async_get_many_related(object(), instance, "tags", None)
        assert result == [1, 2, 3]


class TestLazyResolutionRemaining:
    def test_format_selection_set_none(self):
        assert _format_selection_set(None) == ""

    def test_query_selection_path_inner_only(self):
        posts = SimpleNamespace(
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
        users = SimpleNamespace(
            name=SimpleNamespace(value="users"),
            selection_set=SimpleNamespace(selections=[posts]),
        )
        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=SimpleNamespace(value="Named"),
            selection_set=SimpleNamespace(selections=[users]),
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
        path = _query_selection_path(info)
        assert "Named" in path
        assert "users" in path

    def test_query_selection_path_fallback_inner(self):
        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=None,
            selection_set=SimpleNamespace(selections=[]),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(key="missing", prev=None),
            field_name="missing",
            python_name="missing",
        )
        assert _query_selection_path(info) == "{ missing }"

    def test_parent_graphql_type_missing(self):
        assert _parent_graphql_type(SimpleNamespace(parent_type=None)) == "?"

    def test_django_generic_relation_hint(self):
        from tests.backends.django.models import Post as DjPost

        post = DjPost()
        hint = _django_relation_hint(post, "nonexistent_field_xyz", "Post")
        assert "optimizer eager-loads" in hint

    def test_django_m2m_relation_hint(self):
        from tests.backends.django.models import Post as DjPost

        post = DjPost()
        hint = _django_relation_hint(post, "tags", "Post")
        assert "prefetch_related('tags')" in hint

    def test_query_selection_path_op_type_only(self):
        posts = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=None,
        )
        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=None,
            selection_set=SimpleNamespace(selections=[posts]),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(key="posts", prev=None),
            field_name="posts",
            python_name="posts",
        )
        assert _query_selection_path(info) == "query { posts }"

    def test_prefetch_detection_branches(self, sa_session, seed):
        from tests.backends.django.models import Post as DjPost

        assert _django_relation_prefetched(object(), "missing") is None
        post = DjPost()
        assert _django_relation_prefetched(post, "title") is None

        post2 = sa_session.get(SAPost, 1)
        assert _sqlalchemy_relation_prefetched(object(), "author") is None
        assert _sqlalchemy_relation_prefetched(post2, "missing_rel") is None

        tortoise_instance = SimpleNamespace(
            _meta=SimpleNamespace(
                fields_map={"tags": SimpleNamespace(related_model=object)}
            ),
            _fetched=frozenset({"tags"}),
            __dict__={},
        )
        assert _tortoise_relation_prefetched(tortoise_instance, "tags") is True

        tortoise_dict = SimpleNamespace(
            _meta=SimpleNamespace(
                fields_map={"comments": SimpleNamespace(related_model=object)}
            ),
            _fetched={"comments": True},
            __dict__={},
        )
        assert _tortoise_relation_prefetched(tortoise_dict, "comments") is True

        tortoise_attr = type("TInst", (), {})()
        tortoise_attr._meta = SimpleNamespace(
            fields_map={"author": SimpleNamespace(related_model=object)}
        )
        tortoise_attr._fetched = set()
        tortoise_attr.author = object()
        assert _tortoise_relation_prefetched(tortoise_attr, "author") is True

        class BackwardFKRelation:
            related_model = object

        tortoise_list = SimpleNamespace(
            _meta=SimpleNamespace(fields_map={"posts": BackwardFKRelation()}),
            posts=[object()],
        )
        assert _tortoise_relation_prefetched(tortoise_list, "posts") is True

        assert relation_is_prefetched(DummyCovBackend(), object(), "x") is None

    def test_lazy_extension_skips_when_field_name_missing(self):
        ext = LazyResolutionExtension.configure(DjangoBackend(), mode="warn")()
        info = SimpleNamespace(python_name=None, field_name=None)
        ext._record_relation_access(object(), info)
        assert ext._loads == []


class DummyCovBackend(BaseBackend):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(warn_missing_queryset=False, **kwargs)

    def _introspect_model(self, model: type):
        return [
            ("id", int, False, None),
            ("name", str, False, None),
            ("password_hash", str, False, None),
            ("created_at", __import__("datetime").datetime, False, None),
        ]

    def is_query_object(self, value: Any) -> bool:
        return False


class TestBaseBackendRemaining:
    def test_filter_skips_relation_without_model(self):
        backend = DummyCovBackend()

        def introspect(_model: type):
            return [("orphan", Any, True, None), ("name", str, False, None)]

        with patch.object(backend, "_introspect_model", side_effect=introspect):
            filt = backend.filter(object)
            assert filt is not None

    def test_filter_type_include_exclude_branches(self):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.filter_type(SAUser, include=["name"])
        class IncludedFilter:
            name: auto
            email: auto

        assert IncludedFilter is not None

        @backend.filter_type(SAUser, exclude=["email"])
        class ExcludedFilter:
            name: auto
            email: auto

        assert ExcludedFilter is not None

    def test_order_type_include_exclude_branches(self):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.order_type(SAPost, include=["title"])
        class IncludedOrder:
            title: auto
            body: auto

        assert IncludedOrder is not None

        @backend.order_type(SAPost, exclude=["body"])
        class ExcludedOrder:
            title: auto
            body: auto

        assert ExcludedOrder is not None

    def test_group_include_exclude_and_sensitive_skip(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        group = backend.group(SAUser, include=["name"], exclude=["email"])
        assert group is not None

        @backend.group_type(SAUser, include=["created_at"])
        class DateGroup:
            created_at: auto
            name: auto

        assert DateGroup is not None

        @backend.group_type(SAUser, exclude=["name"])
        class ExcludedGroup:
            name: auto
            created_at: auto

        assert ExcludedGroup is not None

    def test_filter_type_fk_attname_skipped(self):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.filter_type(SAPost)
        class PostFilterWithFk:
            author_id: auto
            title: auto

        fields = PostFilterWithFk._field_type.__dataclass_fields__
        assert "title" in fields
        assert "author_id" not in fields

    def test_group_exclude_and_sensitive_skip(self):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.group_type(SAUser, exclude=["name"])
        class ExcludedGroup:
            name: auto
            email: auto

        group_fields = ExcludedGroup._field_type.__dataclass_fields__
        assert "name" not in group_fields
        assert "email" in group_fields

    def test_lazy_field_definition_skips_check(self):
        backend = SQLAlchemyBackend(dialect="sqlite", lazy_resolution="warn")
        user_filt = backend.filter(SAUser)
        user_order = backend.order(SAUser)

        @backend.type(SAUser, filters=user_filt, order=user_order)
        class UserType:
            id: auto

        @backend.type(SAPost, filters=backend.filter(SAPost))
        class PostType:
            id: auto
            author: UserType = backend.field(disable_optimization=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend.type(SAPost)(PostType)
        assert not any("author" in str(w.message) for w in caught)


class TestSQLAlchemyRemaining:
    def test_invoke_aggregate_handler_with_info(self):
        def handler(_self, columns, *, info=None):
            return columns.id if info is not None else None

        result = _invoke_aggregate_handler(handler, SimpleNamespace(id=1), info="ctx")
        assert result == 1

    def test_introspect_impl_column(self, cov_session):
        meta = _introspect_sa_model(_CovImplCol)
        assert any(row[0] == "flag" and row[1] is bool for row in meta)

    def test_type_decorator_impl_column_and_relation_resolver(self, cov_session):
        from tests.backends.sqlalchemy.fixtures import PostType, UserType

        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.type(_CovImplCol)
        class ImplType:
            id: auto
            flag: auto

        assert ImplType is not None
        assert callable(getattr(UserType, "posts", None))
        assert getattr(PostType, "__orm_filter__", None) is not None

        @backend.type(SAUser)
        class UserWithFakeRel:
            id: auto
            fake_rel: list[PostType]

        assert not callable(getattr(UserWithFakeRel, "fake_rel", None))

    def test_apply_aggregation_empty_agg_cols(self, sa_session, seed, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")

        @strawberry.type
        class Agg:
            count: int

        meta = AggregateMeta(
            model=Post,
            aggregates_type=Agg,
            group_key_type=object,
        )
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="aggregates",
                    selections=[SimpleNamespace(name="sum", selections=[])],
                )
            ]
        )
        result = backend.apply_aggregation(select(Post), info, meta)
        assert result.count == 0

    def test_apply_grouping_empty_group_cols(self, sa_session, seed, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        meta = backend._build_aggregate_types(Post)

        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyGroup:
            field: EmptyField | None = strawberry.UNSET

        groups = backend.apply_grouping(
            select(Post),
            EmptyGroup(),
            SimpleNamespace(selected_fields=[], context={"session": sa_session}),
            meta,
        )
        assert groups == []

    def test_coerce_reference_value_branches(self):
        assert _coerce_reference_value(3) == 3
        assert _coerce_reference_value("abc") == "abc"
        assert _coerce_reference_value(["1", "2"]) == [1, 2]

    def test_build_sa_filter_forward_relation_has(self, Post, User, sa_session):
        from tests.backends.sqlalchemy.fixtures import PostFilter, UserFilter

        user_field = UserFilter._field_type
        obj = PostFilter._object_type(
            author=UserFilter(field=user_field(name=StringLookup(exact="Alice")))
        )
        clause, _ = _build_sa_filter(PostFilter(object=obj), Post)
        assert clause is not None

    def test_extract_sa_group_duplicate_and_overlapping_order(self, Post):
        @strawberry.input
        class GroupField:
            author_id: bool | None = True
            author_id_dup: bool | None = True

        @strawberry.input
        class GroupEntry:
            field: GroupField | None = strawberry.UNSET

        subq = select(Post).subquery()
        _, keys = _extract_sa_group_columns(
            [GroupEntry(field=GroupField(author_id=True, author_id_dup=True))],
            Post,
            subq,
        )
        assert keys == ["author_id"]

        @strawberry.input
        class OrderField:
            author_id: Ordering | None = Ordering.ASC

        @strawberry.input
        class OrderEntry:
            field: OrderField | None = strawberry.UNSET

        clauses = _extract_overlapping_order(
            OrderEntry(field=OrderField(author_id=Ordering.ASC)),
            {"author_id"},
            Post,
            subq,
        )
        assert len(clauses) == 1

    def test_optimizer_leaf_loader_and_only(self, sa_session, seed, User, Post):
        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    load=["posts"],
                    only=["name"],
                    disable_optimization=False,
                )
            }
        }
        rel_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=None,
        )
        field_node = SimpleNamespace(
            selection_set=SimpleNamespace(selections=[rel_sel])
        )
        info = SimpleNamespace(
            field_nodes=[field_node],
            context={"session": sa_session},
        )
        stmt = backend.get_default_queryset(SAUser)
        result = backend.apply_optimizer_hints(backend._store, stmt, info)
        assert result is not None

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_repo_unlink_delete(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(SABase.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as async_session:
            tag = SATag(id=1, name="python")
            post = SAPost(id=1, title="T", body="B", is_published=True, author_id=1)
            post.tags = [tag]
            async_session.add_all([tag, post])
            await async_session.flush()

            class TagRepo(AbstractRepo[SATag]):
                pass

            backend = SQLAlchemyBackend(dialect="sqlite")
            backend._repos = {SATag: TagRepo}
            info = SimpleNamespace(context={"session": async_session})

            @strawberry.input
            class IdRef:
                id: strawberry.ID

            refs = [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=IdRef(id=strawberry.ID("1")),
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=strawberry.UNSET,
                    delete=IdRef(id=strawberry.ID("1")),
                ),
            ]
            await backend.apply_ref_list(post, "tags", refs, info)
        await engine.dispose()

    def test_get_items_arg_list_form_returns_value(self):
        arg = SimpleNamespace(name="first", value=9)
        groups_sel = SimpleNamespace(
            selections=[SimpleNamespace(name="items", arguments=[arg])]
        )
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(name="groups", selections=groups_sel.selections)
            ]
        )
        assert _get_items_arg(info, "first") == 9

    def test_get_items_arg_returns_none_when_missing(self):
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="groups",
                    selections=[SimpleNamespace(name="items", arguments=["after"])],
                )
            ]
        )
        assert _get_items_arg(info, "first") is None


class TestFinal100Coverage:
    def test_sa_type_impl_column_and_introspect(self, cov_session):
        from sqlalchemy.types import Integer, TypeDecorator

        class BoolAsInt(TypeDecorator):
            impl = Integer
            cache_ok = True

        class _ImplModel(_CovBase):
            __tablename__ = "cov_impl_decorator"
            id: Mapped[int] = mapped_column(primary_key=True)
            flag: Mapped[bool] = mapped_column(BoolAsInt())

        backend = SQLAlchemyBackend(dialect="sqlite")

        @backend.type(_ImplModel)
        class ImplDecoratorType:
            id: auto
            flag: auto

        assert ImplDecoratorType is not None
        meta = _introspect_sa_model(_ImplModel)
        assert any(row[0] == "flag" for row in meta)

    def test_lazy_resolution_remaining_branches(self, sa_session, seed):
        from strawberry_orm.lazy_resolution import (
            _django_relation_prefetched,
            _query_selection_path,
            _sqlalchemy_relation_hint,
        )

        operation = SimpleNamespace(
            operation=SimpleNamespace(value="query"),
            name=None,
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="posts"),
                        selection_set=None,
                    )
                ]
            ),
        )
        info = SimpleNamespace(
            operation=operation,
            path=SimpleNamespace(key="posts", prev=None),
            field_name="posts",
            python_name="posts",
        )
        assert _query_selection_path(info) == "query { posts }"

        fk_post = SimpleNamespace(
            _meta=SimpleNamespace(
                get_field=lambda _name: SimpleNamespace(
                    is_relation=True,
                    many_to_one=True,
                    one_to_one=False,
                    is_cached=lambda _inst: True,
                )
            )
        )
        assert _django_relation_prefetched(fk_post, "author") is True

        post = sa_session.get(SAPost, 1)
        assert "selectinload" in _sqlalchemy_relation_hint(post, "comments", "Post")

    def test_base_backend_direct_lazy_and_group_branches(self):
        backend = DummyCovBackend()
        backend._exclude_sensitive_fields = True

        group = backend.group(object, exclude=["name"])
        group_fields = group._field_type.__dataclass_fields__
        assert "name" not in group_fields
        assert "password_hash" not in group_fields
        assert "id" in group_fields

        backend = SQLAlchemyBackend(dialect="sqlite", lazy_resolution="warn")
        user_filt = backend.filter(SAUser)
        user_order = backend.order(SAUser)

        @backend.type(SAUser, filters=user_filt, order=user_order)
        class UserGraph:
            id: auto

        class PostGraph:
            id: auto
            author: UserGraph = FieldDefinition(disable_optimization=True)

        PostGraph.__annotations__ = {"id": int, "author": UserGraph}
        PostGraph.__orm_model__ = SAPost

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            backend._check_lazy_relation_fields(
                PostGraph, SAPost, PostGraph.__annotations__
            )
        assert not any("author" in str(w.message) for w in caught)

    @pytest.mark.django_db
    def test_django_apply_aggregation_and_overlapping_order(self, seed):
        from tests.backends.django.fixtures import PostOrder
        from tests.backends.django.models import Post as DjPost

        backend = DjangoBackend()
        meta = backend._build_aggregate_types(DjPost)
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="aggregates",
                    selections=[SimpleNamespace(name="sum", selections=[])],
                )
            ]
        )
        result = backend.apply_aggregation(DjPost.objects.all(), info, meta)
        assert result.count == 0

        overlap = _extract_django_overlapping_order(
            PostOrder(field=PostOrder._field_type(is_published=Ordering.ASC)),
            {"is_published"},
        )
        assert overlap == ["is_published"]
