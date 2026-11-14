"""Focused backend-adapter coverage for exact helper branches."""

from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import Ordering
from strawberry_orm.backends._base import AggregateMeta
from strawberry_orm.backends.tortoise import (
    TortoiseBackend,
    _apply_python_ordering,
    _build_tortoise_filter,
    _build_tortoise_lookup,
    _build_tortoise_reference_field_clause,
    _build_tortoise_reference_lookup,
    _hide_scoped_out,
    _PlannedPrefetch,
)
from strawberry_orm.filters import ReferenceLookup, StringLookup
from strawberry_orm.grouping import GroupPath, group_paths


@strawberry.input
class EmptyFilterGroup:
    all: list["EmptyFilterGroup"] | None = strawberry.UNSET
    any: list["EmptyFilterGroup"] | None = strawberry.UNSET
    one_of: list["EmptyFilterGroup"] | None = strawberry.UNSET
    not_: "EmptyFilterGroup | None" = strawberry.UNSET
    field: object | None = strawberry.UNSET


@strawberry.input
class RegexLookup:
    regex: str | None = strawberry.UNSET


@strawberry.type
class _TortoiseAggregates:
    count: int


@strawberry.type
class _TortoiseGroupKey:
    author_id: str | None = None


@strawberry.input
class _TortoiseAuthorGroupField:
    author_id: bool | None = True


@strawberry.input
class _TortoiseAuthorGroupBy:
    field: _TortoiseAuthorGroupField | None = strawberry.UNSET


@strawberry.input
class _TortoiseAuthorOrderField:
    author_id: Ordering | None = Ordering.DESC


@strawberry.input
class _TortoiseAuthorOrder:
    field: _TortoiseAuthorOrderField | None = strawberry.UNSET


@strawberry.input
class _TortoiseRefAuthorField:
    author_id: ReferenceLookup | None = strawberry.UNSET


def _tortoise_aggregate_meta(model):
    return AggregateMeta(
        model=model,
        aggregates_type=_TortoiseAggregates,
        group_key_type=_TortoiseGroupKey,
    )


def _tortoise_info_with_aggregates(path: str = "aggregates", **agg_fields):
    selections = []
    if agg_fields.get("count"):
        selections.append(SimpleNamespace(name="count", selections=[]))
    for func_name in ("sum", "avg", "min", "max"):
        names = agg_fields.get(f"{func_name}_fields") or []
        if names:
            selections.append(
                SimpleNamespace(
                    name=func_name,
                    selections=[SimpleNamespace(name=n, selections=[]) for n in names],
                )
            )
    parts = path.split(".")
    node = SimpleNamespace(name=parts[-1], selections=selections)
    for part in reversed(parts[:-1]):
        node = SimpleNamespace(name=part, selections=[node])
    return SimpleNamespace(selected_fields=[node], context={})


class NoModelQuery:
    def __init__(self, items):
        self.items = items

    def __await__(self):
        async def _runner():
            return self.items

        return _runner().__await__()


class Item:
    def __init__(self, value):
        self.value = value


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self):
        assert _build_tortoise_filter(None) == (None, None)
        assert _build_tortoise_filter(EmptyFilterGroup()) == (None, None)
        assert _build_tortoise_filter(EmptyFilterGroup(all=[])) == (None, None)
        assert _build_tortoise_filter(EmptyFilterGroup(any=[])) == (None, None)
        assert _build_tortoise_filter(EmptyFilterGroup(one_of=[])) == (None, None)

        with pytest.raises(ValueError, match="maximum is 0"):
            _build_tortoise_filter(
                EmptyFilterGroup(all=[EmptyFilterGroup()]),
                max_branches=0,
            )
        with pytest.raises(ValueError, match="maximum is 0"):
            _build_tortoise_filter(
                EmptyFilterGroup(one_of=[EmptyFilterGroup()]),
                max_branches=0,
            )
        with pytest.raises(ValueError, match="Regex filters are disabled"):
            _build_tortoise_lookup("name", RegexLookup(regex="a.*"))

    @pytest.mark.asyncio
    async def test_hiding_scoped_out_rows_with_nothing_cached_queries_nothing(
        self, tortoise_db, User
    ):
        def scope(queryset):
            raise AssertionError("nothing cached, so nothing to check")

        load = _PlannedPrefetch(User, "ForeignKeyFieldInstance", scope)
        await _hide_scoped_out([], "author", load)

    @pytest.mark.asyncio
    async def test_a_page_is_ordered_to_the_row(self, tortoise_db, User):
        """The cut ends its order with the key, and never names the key twice."""
        from pypika_tortoise import Order
        from tortoise.functions import Count

        from strawberry_orm.backends.tortoise import _ordered_to_the_row

        def orderings(query):
            return list(_ordered_to_the_row(query)._orderings)

        assert orderings(User.all().order_by("email")) == [
            ("email", Order.asc),
            ("id", Order.asc),
        ]
        by_key = User.all().order_by("-id")
        assert _ordered_to_the_row(by_key) is by_key
        # Tortoise skips the model's ordering on an annotated query, so the
        # key is all there is to order by.
        annotated = User.all().annotate(posts_count=Count("posts"))
        assert orderings(annotated) == [("id", Order.asc)]

    @pytest.mark.asyncio
    async def test_count_query(self, tortoise_db, seed, User):
        backend = TortoiseBackend()
        qs = backend.get_default_queryset(User)
        assert await backend.count_query(qs, info=None) >= 3

    @pytest.mark.asyncio
    async def test_query_object_helpers_handle_fallback_values(self):
        backend = TortoiseBackend()
        plain = NoModelQuery([Item(None), Item(1)])
        result = await backend.apply_optimizer_hints(None, plain, info=None)
        assert [item.value for item in result] == [None, 1]

    def test_internal_queryset_hooks_and_ordering_helpers(self, seed, User):
        backend = TortoiseBackend()
        backend._type_registry["UserType"] = User

        class QueryType:
            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        backend._type_querysets[User] = QueryType.scope_rows
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    scope=lambda qs, info: qs.filter(email__contains="example.com"),
                    disable_optimization=False,
                )
            }
        }

        qs = backend.get_default_queryset(User)
        scoped = backend._apply_nested_queryset(qs, User, "name", User, info=None)
        sql = scoped.sql()
        assert '"name"' in sql
        assert '"email"' in sql

        ordered = _apply_python_ordering(
            [Item(None), Item(1)],
            [("value", False, True, False)],
        )
        assert [item.value for item in ordered] == [None, 1]

        ordered = _apply_python_ordering(
            [Item(None), Item(1)],
            [("value", False, False, True)],
        )
        assert [item.value for item in ordered] == [1, None]

    @pytest.mark.asyncio
    async def test_reference_lookup_and_grouping_helpers(self, Post):
        ref_q = _build_tortoise_reference_lookup(
            "author_id", ReferenceLookup(exact="1", in_list=["1", "2"])
        )
        assert ref_q is not None

        field_q = _build_tortoise_reference_field_clause(
            _TortoiseRefAuthorField(author_id=ReferenceLookup(exact="1")),
            fk_prefix="author_id",
        )
        assert field_q is not None

    @pytest.mark.asyncio
    async def test_apply_aggregation_grouping_scope_and_batch(self, seed, Post, User):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        query = Post.all()

        empty_info = _tortoise_info_with_aggregates()
        result = await backend.apply_aggregation(query, empty_info, meta)
        assert result.count == 0

        count_info = _tortoise_info_with_aggregates(count=True)
        counted = await backend.apply_aggregation(Post.all(), count_info, meta)
        assert counted.count == 4

        group_info = _tortoise_info_with_aggregates(
            path="groups.aggregates", count=True
        )
        groups = await backend.apply_grouping(
            Post.all(),
            group_paths(
                backend,
                Post,
                _TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True)),
            ),
            group_info,
            meta,
        )
        assert len(groups) == 3

        batch_info = SimpleNamespace(context={})
        batched = await backend.batch_group_items(
            Post.all(),
            [GroupPath(alias="_g0", name="author_id", column="author_id")],
            batch_info,
            Post,
            per_group_limit=1,
            order_input=_TortoiseAuthorOrder(
                field=_TortoiseAuthorOrderField(author_id=Ordering.ASC)
            ),
        )
        assert ("1",) in batched
        assert len(batched[("1",)]) == 1

    @pytest.mark.asyncio
    async def test_apply_ref_list_respects_authorize(self, seed, Post, Tag):
        backend = TortoiseBackend()
        post = await Post.get(pk=1)
        before = {tag.name for tag in await post.tags.all()}

        @strawberry.input
        class CreateTagInput:
            name: str

        ref = SimpleNamespace(
            create=CreateTagInput(name="blocked-tag"),
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        info = SimpleNamespace(context={})
        await backend.apply_ref_list(
            post,
            "tags",
            [ref],
            info,
            authorize=lambda action, model, obj_id, _info: False,
        )
        after = {tag.name for tag in await post.tags.all()}
        assert after == before

        @strawberry.input
        class UnlinkInput:
            id: strawberry.ID

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        blocked = [
            SimpleNamespace(
                create=strawberry.UNSET,
                update=UpdateTagInput(id=strawberry.ID("1"), name="nope"),
                unlink=strawberry.UNSET,
                delete=strawberry.UNSET,
            ),
            SimpleNamespace(
                create=strawberry.UNSET,
                update=strawberry.UNSET,
                unlink=UnlinkInput(id=strawberry.ID("1")),
                delete=strawberry.UNSET,
            ),
            SimpleNamespace(
                create=strawberry.UNSET,
                update=strawberry.UNSET,
                unlink=strawberry.UNSET,
                delete=UnlinkInput(id=strawberry.ID("2")),
            ),
        ]
        await backend.apply_ref_list(
            post,
            "tags",
            blocked,
            info,
            authorize=lambda action, model, obj_id, _info: False,
        )
        assert {tag.name for tag in await post.tags.all()} == before

    @pytest.mark.asyncio
    async def test_apply_optimizer_hints_without_queryset_model(self):
        backend = TortoiseBackend()

        class PlainQuery:
            def __init__(self, items):
                self.items = items

            def __await__(self):
                async def _runner():
                    return self.items

                return _runner().__await__()

        ordered = await backend.apply_optimizer_hints(
            None,
            PlainQuery([Item(None), Item(1)]),
            info=None,
        )
        assert [item.value for item in ordered] == [None, 1]

    @pytest.mark.asyncio
    async def test_apply_aggregation_with_sum(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)

        sum_info = _tortoise_info_with_aggregates(
            sum_fields=["author_id"], avg_fields=["author_id"]
        )
        result = await backend.apply_aggregation(Post.all(), sum_info, meta)
        assert result.count >= 0

    @pytest.mark.asyncio
    async def test_apply_aggregation_empty_sum_request(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = _tortoise_info_with_aggregates(sum_fields=[])
        result = await backend.apply_aggregation(Post.all(), info, meta)
        assert result.count == 0

    def test_introspect_annotation_only_relation(self):
        from tests.backends.tortoise.models import User

        meta = TortoiseBackend()._introspect_model(User)
        assert any(name == "posts" and is_rel for name, _, is_rel, _ in meta)

    def test_coerce_reference_value_numeric_passthrough(self):
        from strawberry_orm.backends.tortoise import _coerce_reference_value

        assert _coerce_reference_value(3) == 3
        assert _coerce_reference_value(3.5) == 3.5

    def test_build_tortoise_reference_lookup_branches(self):
        q = _build_tortoise_reference_lookup(
            "author_id",
            ReferenceLookup(
                exact="1",
                neq="2",
                in_list=["1"],
                not_in_list=["3"],
                is_null=True,
            ),
        )
        assert q is not None

    def test_build_tortoise_reference_field_clause_type_error(self):
        with pytest.raises(TypeError, match="Expected ReferenceLookup"):
            _build_tortoise_reference_field_clause(
                _TortoiseRefAuthorField(author_id=StringLookup(exact="1")),
                fk_prefix="author_id",
            )

    def test_build_tortoise_lookup_reference_branch(self):
        q = _build_tortoise_lookup("author_id", ReferenceLookup(exact="1"))
        assert q is not None

    def test_build_tortoise_ordering_nulls_first_skips_sql_clause(self, seed, Post):
        backend = TortoiseBackend()

        @strawberry.input
        class NullsField:
            title: Ordering | None = Ordering.ASC_NULLS_FIRST

        @strawberry.input
        class NullsOrder:
            field: NullsField | None = strawberry.UNSET

        ordered = backend.apply_ordering(
            Post.all(),
            NullsOrder(field=NullsField(title=Ordering.ASC_NULLS_FIRST)),
            Post,
        )
        assert ordered is not None

    @pytest.mark.asyncio
    async def test_apply_aggregation_empty_result_row(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = _tortoise_info_with_aggregates(count=True)
        result = await backend.apply_aggregation(Post.filter(id=-999), info, meta)
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_type_decorator_adds_relation_resolver(self):
        from tests.backends.tortoise.fixtures import UserType as TortoiseUserType

        assert callable(getattr(TortoiseUserType, "posts", None))

    @pytest.mark.asyncio
    async def test_tortoise_final_coverage_lines(self, seed, Post, User):
        from strawberry_orm.backends.tortoise import (
            _build_tortoise_filter,
            _build_tortoise_reference_lookup,
        )

        with pytest.raises(TypeError, match="Expected ReferenceLookup"):
            _build_tortoise_reference_lookup("author_id", StringLookup(exact="x"))

        @strawberry.input
        class EmptyFilter:
            field: object | None = strawberry.UNSET

        clause, query = _build_tortoise_filter(EmptyFilter(), query=Post.all())
        assert clause is None
        assert query is not None

        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = _tortoise_info_with_aggregates(sum_fields=[])
        result = await backend.apply_aggregation(Post.all(), info, meta)
        assert result.count == 0

        group_info = _tortoise_info_with_aggregates(
            path="groups.aggregates", sum_fields=["author_id"]
        )
        groups = await backend.apply_grouping(
            Post.all(),
            group_paths(
                backend,
                Post,
                _TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True)),
            ),
            group_info,
            meta,
        )
        assert len(groups) >= 1

        backend._type_registry["UserType"] = User
        backend._store.hints = {
            "UserType": {
                "posts": SimpleNamespace(
                    scope=lambda qs, info: qs,
                    disable_optimization=False,
                )
            }
        }
        posts_sel = SimpleNamespace(
            name=SimpleNamespace(value="posts"),
            selection_set=SimpleNamespace(
                selections=[
                    SimpleNamespace(
                        name=SimpleNamespace(value="comments"),
                        selection_set=SimpleNamespace(selections=[]),
                    )
                ]
            ),
        )
        field_node = SimpleNamespace(
            selection_set=SimpleNamespace(selections=[posts_sel])
        )
        ordered = await backend.apply_optimizer_hints(
            backend._store,
            User.all().prefetch_related("posts"),
            SimpleNamespace(field_nodes=[field_node]),
        )
        assert len(ordered) >= 1

    def test_tortoise_reference_lookup_in_list_limit(self):
        with pytest.raises(ValueError, match="maximum is"):
            _build_tortoise_reference_lookup(
                "author_id",
                ReferenceLookup(in_list=[str(i) for i in range(501)]),
                max_in_list_size=500,
            )

    @pytest.mark.asyncio
    async def test_tortoise_apply_aggregation_empty_agg_kwargs(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = SimpleNamespace(
            selected_fields=[
                SimpleNamespace(
                    name="aggregates",
                    selections=[SimpleNamespace(name="sum", selections=[])],
                )
            ]
        )
        result = await backend.apply_aggregation(Post.filter(id=-999), info, meta)
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_tortoise_apply_aggregation_empty_rows_with_count(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = _tortoise_info_with_aggregates(count=True)
        result = await backend.apply_aggregation(Post.filter(id=-999), info, meta)
        assert result.count == 0


class TestTortoiseQueryProbe:
    """The diagnostics probe wraps the client's execute methods."""

    def test_probe_without_a_connection_counts_nothing(self):
        backend = TortoiseBackend(warn_missing_scope=False)
        with backend.query_probe(SimpleNamespace(context={})) as probe:
            pass
        assert probe.count == 0

    @pytest.mark.asyncio
    async def test_probe_counts_statements(self, seed, Post):
        backend = TortoiseBackend(warn_missing_scope=False)
        with backend.query_probe(SimpleNamespace(context={})) as probe:
            await Post.all()
        assert probe.count == 1


class TestQueryOrderingRegistry:
    """Python-side orderings are keyed by ``id()``, which CPython recycles.

    Without an identity check a brand-new queryset could inherit the ordering
    of a dead one, silently reordering another query's rows.
    """

    def test_ordering_round_trips_for_the_same_object(self):
        from strawberry_orm.backends.tortoise import (
            _query_orderings,
            _remember_query_ordering,
        )

        query = object()
        ordering = [("title", True, None, None)]
        _remember_query_ordering(query, ordering)
        assert _query_orderings(query) == ordering

    def test_recycled_id_does_not_inherit_a_stale_ordering(self):
        from strawberry_orm.backends.tortoise import (
            _QUERY_ORDERINGS,
            _query_orderings,
        )

        fresh = object()
        # Simulate a dead query whose id was handed to ``fresh``.
        _QUERY_ORDERINGS[id(fresh)] = (object(), [("stale", True, None, None)])
        assert _query_orderings(fresh) is None

    def test_registry_is_bounded(self):
        from strawberry_orm.backends.tortoise import (
            _MAX_REMEMBERED_ORDERINGS,
            _QUERY_ORDERINGS,
            _remember_query_ordering,
        )

        keepalive = [object() for _ in range(_MAX_REMEMBERED_ORDERINGS + 50)]
        for item in keepalive:
            _remember_query_ordering(item, [("title", True, None, None)])
        assert len(_QUERY_ORDERINGS) <= _MAX_REMEMBERED_ORDERINGS


class TestRelationScopeCondition:
    def test_without_a_backend_there_is_nothing_to_scope(self):
        from strawberry_orm.backends.tortoise import _tortoise_relation_scope_q

        assert _tortoise_relation_scope_q(None, None, "author", None, "") is None


class TestGroupedRows:
    async def _keys(self, Post, custom):
        from strawberry_orm.backends.tortoise import _GroupedRows

        path = GroupPath(alias="_g0", name="by", custom=custom, value=True)
        rows = _GroupedRows(
            TortoiseBackend(), Post.all(), Post, [path], SimpleNamespace(context={})
        )
        found = await rows.execute(rows.stmt.select(*rows.selected()))
        return sorted({str(row["_g0"]) for row in found})

    async def test_a_group_field_may_name_a_column_through_a_relation(self, seed, Post):
        keys = await self._keys(Post, lambda self, value, query: "author__name")
        assert keys == ["Alice", "Bob", "Charlie"]

    async def test_a_group_field_may_return_a_pypika_term(self, seed, Post):
        from pypika_tortoise.terms import Field

        keys = await self._keys(Post, lambda self, value, query: Field("author_id"))
        assert keys == ["1", "2", "3"]

    def test_a_period_is_cut_by_postgres_and_refused_elsewhere(self, Post):
        from pypika_tortoise import Query
        from pypika_tortoise.terms import Field

        from strawberry_orm.backends.tortoise import _truncate
        from strawberry_orm.types import DateGroupByInterval

        def sql(field):
            return str(Query.select(_truncate(term, field, month, "postgres")))

        created = Post._meta.fields_map["created_at"]
        month = DateGroupByInterval.MONTH
        term = Field("created_at")
        assert sql(created) == "SELECT DATE_TRUNC('month',\"created_at\")"
        assert (
            sql(object()) == "SELECT CAST(DATE_TRUNC('month',\"created_at\") AS DATE)"
        )
        with pytest.raises(ValueError, match="not mysql"):
            _truncate(term, created, month, "mysql")
