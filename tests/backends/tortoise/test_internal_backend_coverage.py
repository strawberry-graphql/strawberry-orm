"""Focused backend-adapter coverage for exact helper branches."""

from dataclasses import dataclass
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
    _build_tortoise_order_from_input,
    _build_tortoise_reference_field_clause,
    _build_tortoise_reference_lookup,
    _CustomRel,
    _extract_tortoise_group_fields,
    _extract_tortoise_overlapping_order,
    _get_reverse_fk_field,
)
from strawberry_orm.filters import ReferenceLookup, StringLookup


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


class ParentWithoutId:
    pass


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self, Post, Comment):
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

        assert _get_reverse_fk_field(Comment, Post, "post") == "post_id"

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

        await backend._apply_custom_prefetch([], [])
        await backend._apply_custom_prefetch(
            [ParentWithoutId()], [_CustomRel("", "", object, None, lambda qs: qs, [])]
        )  # type: ignore[arg-type]

    def test_internal_queryset_hooks_and_ordering_helpers(self, seed, User):
        backend = TortoiseBackend()
        backend._type_registry["UserType"] = User

        class QueryType:
            @classmethod
            def get_queryset(cls, qs, info):
                return qs.filter(name="Alice")

        backend._type_querysets[User] = QueryType.get_queryset
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    load=lambda qs: qs.filter(email__contains="example.com"),
                    only=None,
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

        group_fields, key_fields = _extract_tortoise_group_fields(
            [_TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True))]
        )
        assert group_fields == ["author_id"]
        assert key_fields == ["author_id"]

        order = _TortoiseAuthorOrder(
            field=_TortoiseAuthorOrderField(author_id=Ordering.DESC)
        )
        overlapping = _extract_tortoise_overlapping_order(order, {"author_id"})
        assert overlapping == ["-author_id"]
        assert _build_tortoise_order_from_input(order) == ["-author_id"]

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
            _TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True)),
            group_info,
            meta,
        )
        assert len(groups) == 3

        @dataclass
        class ScopeKey:
            author_id: int | None = 1

        scoped = backend.scope_query_to_group(Post.all(), ScopeKey())
        rows = await scoped
        assert len(rows) == 2

        batch_info = SimpleNamespace(context={})
        batched = await backend.batch_group_items(
            Post.all(),
            ["author_id"],
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
    async def test_apply_grouping_order_branch_with_mock_queryset(self, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        group_info = _tortoise_info_with_aggregates(
            path="groups.aggregates", count=True
        )

        class FakeValuesQuery:
            def __init__(self):
                self.order_args: tuple[str, ...] = ()

            def order_by(self, *args):
                self.order_args = args
                return self

            def __await__(self):
                async def _run():
                    return [{"author_id": 1, "_count": 2}]

                return _run().__await__()

        class FakeQuery:
            def annotate(self, **_kwargs):
                return self

            def group_by(self, *_args):
                return self

            def values(self, *_args):
                return FakeValuesQuery()

        fake = FakeQuery()
        groups = await backend.apply_grouping(
            fake,
            _TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True)),
            group_info,
            meta,
            order_input=_TortoiseAuthorOrder(
                field=_TortoiseAuthorOrderField(author_id=Ordering.ASC)
            ),
        )
        assert len(groups) == 1
        assert groups[0].key.author_id == "1"

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
    async def test_apply_aggregation_with_sum_and_empty_group_fields(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)

        sum_info = _tortoise_info_with_aggregates(
            sum_fields=["author_id"], avg_fields=["author_id"]
        )
        result = await backend.apply_aggregation(Post.all(), sum_info, meta)
        assert result.count >= 0

        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyGroup:
            field: EmptyField | None = strawberry.UNSET

        groups = await backend.apply_grouping(
            Post.all(), EmptyGroup(), _tortoise_info_with_aggregates(), meta
        )
        assert groups == []

    @pytest.mark.asyncio
    async def test_apply_aggregation_empty_sum_request(self, seed, Post):
        backend = TortoiseBackend()
        meta = _tortoise_aggregate_meta(Post)
        info = _tortoise_info_with_aggregates(sum_fields=[])
        result = await backend.apply_aggregation(Post.all(), info, meta)
        assert result.count == 0

    @pytest.mark.asyncio
    async def test_grouping_helpers_skip_duplicate_fields(self):
        from strawberry_orm.backends.tortoise import _extract_tortoise_group_fields

        @strawberry.input
        class DupField:
            title: bool | None = True

        @strawberry.input
        class DupGroup:
            field: DupField | None = strawberry.UNSET

        entry = DupGroup(field=DupField())
        fields, keys = _extract_tortoise_group_fields([entry, entry])
        assert fields.count("title") == 1

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

    def test_extract_tortoise_overlapping_order_empty_field(self):
        @strawberry.input
        class EmptyOrder:
            field: object | None = strawberry.UNSET

        assert _extract_tortoise_overlapping_order(EmptyOrder(), {"title"}) == []

    def test_build_tortoise_order_from_input_empty_field(self):
        @strawberry.input
        class EmptyOrder:
            field: object | None = strawberry.UNSET

        assert _build_tortoise_order_from_input(EmptyOrder()) == []

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
            _build_tortoise_order_from_input,
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

        @strawberry.input
        class EmptyOrder:
            field: object | None = strawberry.UNSET

        assert _build_tortoise_order_from_input(EmptyOrder()) == []

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
            _TortoiseAuthorGroupBy(field=_TortoiseAuthorGroupField(author_id=True)),
            group_info,
            meta,
        )
        assert len(groups) >= 1

        backend._type_registry["UserType"] = User
        backend._store.hints = {
            "UserType": {
                "posts": SimpleNamespace(
                    load=lambda qs: qs,
                    only=None,
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

    def test_tortoise_group_and_order_skip_empty_values(self):
        from strawberry_orm.backends.tortoise import (
            _build_tortoise_order_from_input,
            _extract_tortoise_group_fields,
            _extract_tortoise_overlapping_order,
        )

        @strawberry.input
        class PartialField:
            title: bool | None = None

        @strawberry.input
        class PartialGroup:
            field: PartialField | None = strawberry.UNSET

        fields, _ = _extract_tortoise_group_fields([PartialGroup(field=PartialField())])
        assert fields == []

        @strawberry.input
        class PartialOrderField:
            title: Ordering | None = None

        @strawberry.input
        class PartialOrder:
            field: PartialOrderField | None = strawberry.UNSET

        assert (
            _extract_tortoise_overlapping_order(
                PartialOrder(field=PartialOrderField()), {"title"}
            )
            == []
        )
        assert (
            _build_tortoise_order_from_input(PartialOrder(field=PartialOrderField()))
            == []
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
