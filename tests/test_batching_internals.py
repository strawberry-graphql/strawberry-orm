"""Unit tests for batching internals: the reflection primitives and bail paths.

The differential harness proves batched output matches per-row output for the
shapes that batch. These cover the refusals directly, so a future change that
accidentally starts rewriting an unsafe shape fails here rather than silently
returning the wrong rows.
"""

from types import SimpleNamespace

from sqlalchemy import or_, select

from strawberry_orm.backends._base import BaseBackend
from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from strawberry_orm.backends.tortoise import TortoiseBackend
from strawberry_orm.batching import (
    BatchingExtension,
    _operation_store,
    extensions_include_batching,
    path_key,
    stash_parents,
)
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class _MinimalBackend(BaseBackend):
    """Exercises the conservative defaults inherited by non-reflecting backends."""

    def __init__(self):
        super().__init__(warn_missing_scope=False)

    def _introspect_model(self, model):  # pragma: no cover - unused
        return []


class TestBackendDefaultsRefuseToBatch:
    def test_instance_pk_defaults_to_none(self):
        assert _MinimalBackend().instance_pk(object()) is None

    def test_split_parent_predicate_defaults_to_none(self):
        assert _MinimalBackend().split_parent_predicate(object(), 1) is None

    def test_query_signature_defaults_to_none(self):
        assert _MinimalBackend().query_signature(object()) is None

    def test_tortoise_inherits_the_refusing_defaults(self):
        backend = TortoiseBackend(warn_missing_scope=False)
        assert backend.instance_pk(object()) is None
        assert backend.split_parent_predicate(object(), 1) is None
        assert backend.query_signature(object()) is None


class TestSQLAlchemyReflection:
    def _backend(self):
        return SQLAlchemyBackend(dialect="sqlite", warn_missing_scope=False)

    def test_splits_a_plain_parent_predicate(self):
        stmt = select(SAPost).where(SAPost.author_id == 1)
        attr, handle, remainder = self._backend().split_parent_predicate(stmt, 1)
        assert attr == "author_id"
        assert handle is not None
        assert remainder._where_criteria == ()

    def test_keeps_other_criteria_in_the_remainder(self):
        stmt = select(SAPost).where(
            SAPost.author_id == 1, SAPost.is_published.is_(True)
        )
        attr, _, remainder = self._backend().split_parent_predicate(stmt, 1)
        assert attr == "author_id"
        assert len(remainder._where_criteria) == 1

    def test_boolean_column_is_not_mistaken_for_the_parent_key(self):
        """``True == 1`` must not make is_published look like author_id."""
        stmt = select(SAPost).where(SAPost.is_published.is_(True))
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_non_relation_equality_is_skipped(self):
        """An ``==`` on a plain column is not a candidate for the parent key."""
        stmt = select(SAPost).where(SAPost.author_id == 1, SAPost.title == "x")
        attr, _, remainder = self._backend().split_parent_predicate(stmt, 1)
        assert attr == "author_id"
        assert len(remainder._where_criteria) == 1

    def test_bails_when_the_entity_cannot_be_determined(self):
        """Without a mapped entity there is no table to anchor the key to."""
        from sqlalchemy import literal_column

        stmt = select(literal_column("1")).where(SAPost.author_id == 1)
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_foreign_key_on_another_table_is_skipped(self):
        """A joined table's FK column is not this row's own parent key."""
        stmt = (
            select(SAPost)
            .join(SAComment, SAComment.post_id == SAPost.id)
            .where(SAComment.author_id == 1)
        )
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_bails_on_limit(self):
        stmt = select(SAPost).where(SAPost.author_id == 1).limit(5)
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_bails_on_offset(self):
        stmt = select(SAPost).where(SAPost.author_id == 1).offset(5)
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_bails_without_any_criteria(self):
        assert self._backend().split_parent_predicate(select(SAPost), 1) is None

    def test_bails_when_the_parent_key_is_inside_an_or(self):
        stmt = select(SAPost).where(
            or_(SAPost.author_id == 1, SAPost.title == "nothing")
        )
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_bails_when_no_predicate_matches_the_parent(self):
        stmt = select(SAPost).where(SAPost.author_id == 99)
        assert self._backend().split_parent_predicate(stmt, 1) is None

    def test_signature_is_stable_for_equal_remainders(self):
        backend = self._backend()
        first = select(SAPost).where(SAPost.is_published.is_(True))
        second = select(SAPost).where(SAPost.is_published.is_(True))
        assert backend.query_signature(first) == backend.query_signature(second)

    def test_signature_differs_for_different_remainders(self):
        backend = self._backend()
        first = select(SAPost).where(SAPost.is_published.is_(True))
        second = select(SAPost).where(SAPost.is_published.is_(False))
        assert backend.query_signature(first) != backend.query_signature(second)

    def test_instance_pk_is_none_for_a_transient_instance(self):
        assert self._backend().instance_pk(SAUser(name="x", email="y")) is None

    def test_instance_pk_is_none_for_a_non_orm_object(self):
        assert self._backend().instance_pk(object()) is None

    def test_apply_key_filter_adds_an_in_clause(self):
        backend = self._backend()
        stmt = select(SAPost)
        filtered = backend.apply_key_filter(stmt, "author_id", SAPost.author_id, [1, 2])
        assert "IN" in str(filtered).upper()


class TestBatchingHelpers:
    def test_extensions_include_batching_detects_configured_subclass(self):
        backend = SQLAlchemyBackend(dialect="sqlite", warn_missing_scope=False)
        configured = BatchingExtension.configure(backend, backend._store)
        assert extensions_include_batching([configured]) is True
        assert extensions_include_batching([BatchingExtension]) is True
        assert extensions_include_batching([object()]) is False

    def test_operation_store_without_a_context_is_none(self):
        assert _operation_store(None, "anything") is None

    def test_stash_parents_without_a_context_is_a_no_op(self):
        info = SimpleNamespace(
            path=SimpleNamespace(key="users", prev=None),
            field_name="users",
            python_name="users",
        )
        stash_parents(None, info, [object(), object()])  # must not raise

    def test_extensions_include_batching_matches_instances_by_name(self):
        assert extensions_include_batching(
            [SimpleNamespace(__name__="BatchingExtension_DjangoBackend")]
        )

    def test_operation_store_reuses_the_same_dict(self):
        ctx = SimpleNamespace()
        first = _operation_store(ctx, "key")
        first["a"] = 1
        assert _operation_store(ctx, "key") == {"a": 1}

    def test_path_key_drops_list_indices(self):
        path = SimpleNamespace(
            key="posts",
            prev=SimpleNamespace(key=0, prev=SimpleNamespace(key="users", prev=None)),
        )
        info = SimpleNamespace(path=path, field_name="posts", python_name="posts")
        assert path_key(info) == "users.posts"

    def test_stash_parents_ignores_non_lists_and_short_lists(self):
        ctx = SimpleNamespace()
        info = SimpleNamespace(
            path=SimpleNamespace(key="users", prev=None),
            field_name="users",
            python_name="users",
        )
        stash_parents(ctx, info, "not a list")
        stash_parents(ctx, info, [object()])
        assert getattr(ctx, "_orm_batch_parents", None) is None

    def test_stash_parents_records_multiple_rows(self):
        ctx = SimpleNamespace()
        info = SimpleNamespace(
            path=SimpleNamespace(key="users", prev=None),
            field_name="users",
            python_name="users",
        )
        rows = [object(), object()]
        stash_parents(ctx, info, rows)
        assert ctx._orm_batch_parents == {"users": rows}


class TestBatchingExtensionGuards:
    def _extension(self):
        backend = SQLAlchemyBackend(dialect="sqlite", warn_missing_scope=False)
        return BatchingExtension.configure(backend, backend._store)()

    def test_root_none_is_passed_through(self):
        ext = self._extension()
        assert ext.resolve(lambda *a, **k: "value", None, SimpleNamespace()) == "value"

    def test_missing_backend_is_passed_through(self):
        ext = BatchingExtension()
        assert (
            ext.resolve(lambda *a, **k: "value", object(), SimpleNamespace()) == "value"
        )

    def test_non_orm_return_type_is_passed_through(self):
        ext = self._extension()
        info = SimpleNamespace(return_type=None)
        assert ext.resolve(lambda *a, **k: "value", object(), info) == "value"

    def _orm_list_info(self, path):
        """Info whose return type is ``[PT]`` with PT registered as an ORM type."""
        from graphql import GraphQLList, GraphQLObjectType, GraphQLString

        object_type = GraphQLObjectType(
            "PT", {"title": SimpleNamespace(type=GraphQLString)}
        )
        return SimpleNamespace(
            return_type=GraphQLList(object_type),
            path=path,
            field_name="posts",
            python_name="posts",
        )

    def _registered_extension(self):
        backend = SQLAlchemyBackend(dialect="sqlite", warn_missing_scope=False)
        backend._type_registry["PT"] = SAPost
        return BatchingExtension.configure(backend, backend._store)()

    def test_missing_execution_context_is_passed_through(self):
        ext = self._registered_extension()
        info = self._orm_list_info(SimpleNamespace(key="posts", prev=None))
        assert ext.resolve(lambda *a, **k: ["row"], object(), info) == ["row"]

    def test_root_level_field_is_not_batched(self):
        ext = self._registered_extension()
        ext.execution_context = SimpleNamespace()
        info = self._orm_list_info(SimpleNamespace(key="posts", prev=None))
        assert ext.resolve(lambda *a, **k: ["row"], object(), info) == ["row"]

    def test_field_without_stashed_parents_is_not_batched(self):
        ext = self._registered_extension()
        ext.execution_context = SimpleNamespace()
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)
        assert ext.resolve(lambda *a, **k: ["row"], object(), info) == ["row"]

    def test_single_parent_is_not_batched(self):
        ext = self._registered_extension()
        root = object()
        ext.execution_context = SimpleNamespace(
            _orm_batch_parents={"users": [root]},
        )
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)
        assert ext.resolve(lambda *a, **k: ["row"], root, info) == ["row"]

    def test_root_outside_the_stashed_parents_is_not_batched(self):
        ext = self._registered_extension()
        ext.execution_context = SimpleNamespace(
            _orm_batch_parents={"users": [object(), object()]},
        )
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)
        assert ext.resolve(lambda *a, **k: ["row"], object(), info) == ["row"]

    def test_materializing_resolver_bails_without_running_ahead(self):
        ext = self._registered_extension()
        root = object()
        parents = [root, object()]
        ext.execution_context = SimpleNamespace(_orm_batch_parents={"users": parents})
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)

        calls = []

        def _next(parent, _info, *a, **k):
            calls.append(parent)
            return ["already materialized"]

        assert ext.resolve(_next, root, info) == ["already materialized"]
        # Only this parent was resolved; siblings were never run speculatively.
        assert calls == [root]

    def test_sibling_returning_rows_bails(self, monkeypatch):
        ext = self._registered_extension()
        root = SAUser(id=1, name="a", email="b")
        sibling = SAUser(id=2, name="c", email="d")
        ext.execution_context = SimpleNamespace(
            _orm_batch_parents={"users": [root, sibling]}
        )
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)
        monkeypatch.setattr(ext._backend, "instance_pk", lambda instance: instance.id)

        stmt = select(SAPost).where(SAPost.author_id == 1)

        def _next(parent, _info, *a, **k):
            return stmt if parent is root else ["already materialized"]

        assert ext.resolve(_next, root, info) is stmt

    def test_parent_without_a_primary_key_bails(self):
        ext = self._registered_extension()
        root = object()
        ext.execution_context = SimpleNamespace(
            _orm_batch_parents={"users": [root, object()]}
        )
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)

        stmt = select(SAPost).where(SAPost.author_id == 1)
        assert ext.resolve(lambda *a, **k: stmt, root, info) is stmt

    def test_unsignable_remainder_bails(self, monkeypatch):
        ext = self._registered_extension()
        root = SAUser(id=1, name="a", email="b")
        sibling = SAUser(id=2, name="c", email="d")
        ext.execution_context = SimpleNamespace(
            _orm_batch_parents={"users": [root, sibling]}
        )
        path = SimpleNamespace(
            key="posts", prev=SimpleNamespace(key="users", prev=None)
        )
        info = self._orm_list_info(path)

        monkeypatch.setattr(ext._backend, "instance_pk", lambda instance: instance.id)
        monkeypatch.setattr(ext._backend, "query_signature", lambda query: None)

        def _next(parent, _info, *a, **k):
            return select(SAPost).where(SAPost.author_id == parent.id)

        result = ext.resolve(_next, root, info)
        assert result is not None  # bailed back to this parent's own statement
