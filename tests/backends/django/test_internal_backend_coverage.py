"""Focused backend-adapter coverage for exact helper branches."""

from types import SimpleNamespace

import pytest
import strawberry
from django.db.models import Q

from strawberry_orm import StrawberryORM
from strawberry_orm._async import await_maybe
from strawberry_orm.backends.django import (
    DjangoBackend,
    _build_django_field_clause,
    _build_django_filter,
    _build_django_lookup,
    _build_django_order_from_input,
    _build_django_reference_field_clause,
    _build_django_reference_lookup,
    _coerce_reference_value,
    _django_forward_fk_attname,
    _django_related_model,
    _extract_django_group_fields,
    _extract_django_overlapping_order,
    _primary_key,
)
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
    filter_tree_uses_only_reference_lookups,
)
from strawberry_orm.filters import IntComparisonLookup, ReferenceLookup, StringLookup
from strawberry_orm.types import Ordering, auto
from tests.backends.django.fixtures import PostFilter, PostOrder, UserFilter
from tests.backends.django.models import Book, Publisher, User


def _mock_info_with_aggregates(
    path: str = "aggregates",
    *,
    count: bool = False,
    sum_fields: list[str] | None = None,
    avg_fields: list[str] | None = None,
    min_fields: list[str] | None = None,
    max_fields: list[str] | None = None,
) -> SimpleNamespace:
    """Build ``info.selected_fields`` for :func:`requested_aggregates`."""
    selections: list[SimpleNamespace] = []
    if count:
        selections.append(SimpleNamespace(name="count", selections=[]))
    for func_name, names in (
        ("sum", sum_fields or []),
        ("avg", avg_fields or []),
        ("min", min_fields or []),
        ("max", max_fields or []),
    ):
        if names:
            selections.append(
                SimpleNamespace(
                    name=func_name,
                    selections=[SimpleNamespace(name=n, selections=[]) for n in names],
                )
            )

    node = SimpleNamespace(name=path.split(".")[-1], selections=selections)
    for part in reversed(path.split(".")[:-1]):
        node = SimpleNamespace(name=part, selections=[node])
    return SimpleNamespace(selected_fields=[node])


@strawberry.input
class EmptyFilterGroup:
    all: list["EmptyFilterGroup"] | None = strawberry.UNSET
    any: list["EmptyFilterGroup"] | None = strawberry.UNSET
    one_of: list["EmptyFilterGroup"] | None = strawberry.UNSET
    not_: "EmptyFilterGroup | None" = strawberry.UNSET
    field: object | None = strawberry.UNSET


@strawberry.input
class BadReferenceField:
    name: str | None = strawberry.UNSET


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self):
        assert _build_django_filter(None) == (None, None)
        assert _build_django_filter(EmptyFilterGroup(all=[])) == (None, None)
        assert _build_django_filter(EmptyFilterGroup(any=[])) == (None, None)
        assert _build_django_filter(EmptyFilterGroup(one_of=[])) == (None, None)

        with pytest.raises(ValueError, match="maximum is 0"):
            _build_django_filter(
                EmptyFilterGroup(all=[EmptyFilterGroup()]), max_branches=0
            )
        with pytest.raises(ValueError, match="maximum is 0"):
            _build_django_filter(
                EmptyFilterGroup(one_of=[EmptyFilterGroup()]), max_branches=0
            )

    def test_query_object_helpers_handle_fallback_values(self, User):
        backend = DjangoBackend()
        plain = ["a", "b"]
        assert backend.apply_optimizer_hints(None, plain, info=None) == plain

        class Info:
            field_nodes = [SimpleNamespace(selection_set=None)]

        assert (
            backend.apply_optimizer_hints(
                None, backend.get_default_queryset(User), Info()
            )
            is not None
        )

    def test_internal_queryset_hooks_and_ordering_helpers(self, User):
        backend = DjangoBackend()
        qs = backend.get_default_queryset(User)
        filtered = backend.apply_filters(qs, None, User)
        ordered = backend.apply_ordering(qs, [], User)
        assert backend.is_query_object(qs) is True
        assert filtered is qs
        assert ordered is qs
        assert _build_django_filter(EmptyFilterGroup()) == (None, None)
        assert str(_build_django_field_clause(EmptyFilterGroup())) == "(AND: )"

    def test_primary_key_and_wrap_async_safe(self):
        assert _primary_key(SimpleNamespace(pk=1)) == 1
        assert _primary_key(SimpleNamespace(id=2)) == 2
        assert _primary_key(SimpleNamespace()) is None

        def resolver() -> int:
            return 42

        async_safe_backend = DjangoBackend()
        wrapped = async_safe_backend.wrap_async_safe(resolver)
        assert wrapped is not resolver

        plain_backend = DjangoBackend(django_async_safe=False)
        assert plain_backend.wrap_async_safe(resolver) is resolver

    def test_django_fk_introspection_helpers(self, Post):
        assert _django_forward_fk_attname(Post, "author") == "author_id"
        assert _django_related_model(Post, "author") is User
        assert _django_forward_fk_attname(Post, "missing") is None
        assert _django_related_model(Post, "missing") is None
        assert _django_forward_fk_attname(Post, "title") is None

    def test_django_fk_introspection_custom_pk(self):
        assert _django_forward_fk_attname(Book, "publisher") == "publisher_id"
        assert _django_related_model(Book, "publisher") is Publisher

    def test_coerce_reference_value_and_reference_lookups(self):
        assert _coerce_reference_value(7) == 7
        assert _coerce_reference_value("42") == 42
        assert _coerce_reference_value("abc") == "abc"
        assert _coerce_reference_value(["1", "2"]) == [1, 2]

        exact_q = _build_django_reference_lookup(
            "author_id", ReferenceLookup(exact="1", neq="2", is_null=True)
        )
        assert exact_q == Q(author_id__exact=1) & ~Q(author_id__exact=2) & Q(
            author_id__isnull="True"
        )

        list_q = _build_django_reference_lookup(
            "author_id",
            ReferenceLookup(in_list=["1", "2"], not_in_list=["3"]),
        )
        assert list_q == Q(author_id__in=[1, 2]) & ~Q(author_id__in=[3])

        with pytest.raises(ValueError, match="maximum is 2"):
            _build_django_reference_lookup(
                "author_id",
                ReferenceLookup(in_list=["1", "2", "3"]),
                max_in_list_size=2,
            )
        with pytest.raises(TypeError, match="ReferenceLookup"):
            _build_django_reference_lookup("author_id", IntComparisonLookup(exact=1))

        fk_clause = _build_django_reference_field_clause(
            UserFilter._field_type(id=ReferenceLookup(exact="1")),
            fk_prefix="author_id",
        )
        assert fk_clause == Q(author_id__exact=1)

        shortcut_clause = _build_django_reference_field_clause(
            UserFilter._field_type(id=IntComparisonLookup(exact=1)),
            fk_prefix="author_id",
        )
        assert shortcut_clause == Q(author_id__exact=1)

        with pytest.raises(TypeError, match="ReferenceLookup or FK-mappable"):
            _build_django_reference_field_clause(
                BadReferenceField(name="x"),
                fk_prefix="author_id",
            )

    def test_build_django_filter_reference_shortcut_and_combinators(self, Post, seed):
        user_field = UserFilter._field_type
        post_object = PostFilter._object_type

        id_filter = PostFilter(
            object=post_object(
                author=UserFilter(field=user_field(id=ReferenceLookup(in_list=["1"])))
            )
        )
        id_clause, _ = _build_django_filter(id_filter, model=Post)
        assert id_clause == Q(author_id__in=[1])

        combined_filter = PostFilter(
            object=post_object(
                author=UserFilter(
                    all=[
                        UserFilter(field=user_field(id=ReferenceLookup(exact="1"))),
                        UserFilter(field=user_field(id=ReferenceLookup(exact="2"))),
                    ]
                )
            )
        )
        assert filter_tree_uses_only_reference_lookups(
            combined_filter.object.author  # type: ignore[union-attr]
        )
        combined_clause, _ = _build_django_filter(combined_filter, model=Post)
        assert combined_clause == Q(author_id__exact=1) & Q(author_id__exact=2)

        any_filter = PostFilter(
            object=post_object(
                author=UserFilter(
                    any=[
                        UserFilter(field=user_field(id=ReferenceLookup(exact="1"))),
                        UserFilter(field=user_field(id=ReferenceLookup(exact="3"))),
                    ]
                )
            )
        )
        any_clause, _ = _build_django_filter(any_filter, model=Post)
        assert any_clause == Q(author_id__exact=1) | Q(author_id__exact=3)

        not_filter = PostFilter(
            object=post_object(
                author=UserFilter(
                    not_=UserFilter(field=user_field(id=ReferenceLookup(exact="1")))
                )
            )
        )
        not_clause, _ = _build_django_filter(not_filter, model=Post)
        assert not_clause == ~Q(author_id__exact=1)

        one_of_filter = PostFilter(
            object=post_object(
                author=UserFilter(
                    one_of=[
                        UserFilter(field=user_field(id=ReferenceLookup(exact="1"))),
                        UserFilter(field=user_field(id=ReferenceLookup(exact="3"))),
                    ]
                )
            )
        )
        one_of_clause, _ = _build_django_filter(one_of_filter, model=Post)
        assert one_of_clause == Q(author_id__exact=1) | Q(author_id__exact=3)

        join_filter = PostFilter(
            object=post_object(
                author=UserFilter(field=user_field(name=StringLookup(exact="Alice")))
            )
        )
        join_clause, _ = _build_django_filter(join_filter, model=Post)
        assert "author__name" in str(join_clause)

        qs = Post.objects.filter(id_clause)
        assert qs.count() == 2

    def test_build_django_filter_custom_pk_reference_shortcut(self):
        @strawberry.input
        class PublisherRefField:
            publisher_code: ReferenceLookup | None = strawberry.UNSET

        @strawberry.input
        class PublisherRefFilter:
            field: PublisherRefField | None = strawberry.UNSET
            all: list["PublisherRefFilter"] | None = strawberry.UNSET

        @strawberry.input
        class BookRefObject:
            publisher: PublisherRefFilter | None = strawberry.UNSET

        @strawberry.input
        class BookRefFilter:
            object: BookRefObject | None = strawberry.UNSET

        filter_input = BookRefFilter(
            object=BookRefObject(
                publisher=PublisherRefFilter(
                    field=PublisherRefField(publisher_code=ReferenceLookup(exact="ACE"))
                )
            )
        )
        clause, _ = _build_django_filter(filter_input, model=Book)
        assert clause == Q(publisher_id__exact="ACE")

        nested = build_reference_object_filter_clause(
            PublisherRefFilter(
                field=PublisherRefField(publisher_code=ReferenceLookup(exact="ACE"))
            ),
            build_field_clause=_build_django_reference_field_clause,
            fk_prefix="publisher_id",
        )
        assert nested == Q(publisher_id__exact="ACE")

    def test_grouping_helper_functions(self, Post):
        orm = StrawberryORM.for_django(lazy_resolution="off")
        post_group = orm.group(Post)
        post_order = orm.order(Post)
        group_field = post_group._field_type
        order_field = post_order._field_type

        group_fields, key_fields = _extract_django_group_fields(
            [post_group(field=group_field(is_published=True))]
        )
        assert group_fields == ["is_published"]
        assert key_fields == ["is_published"]

        overlapping = _extract_django_overlapping_order(
            [post_order(field=order_field(is_published=Ordering.DESC))],
            {"is_published"},
        )
        assert overlapping == ["-is_published"]
        assert (
            _extract_django_overlapping_order(
                [post_order(field=order_field(title=Ordering.ASC))],
                {"is_published"},
            )
            == []
        )

        desc_exprs = _build_django_order_from_input(
            [post_order(field=order_field(title=Ordering.DESC))]
        )
        assert "descending=True" in str(desc_exprs[0])

        order_exprs = _build_django_order_from_input(
            [post_order(field=order_field(title=Ordering.ASC))]
        )
        assert len(order_exprs) == 1
        assert "descending=False" in str(order_exprs[0])

        fallback_exprs = _build_django_order_from_input([])
        assert len(fallback_exprs) == 1
        assert str(fallback_exprs[0]) == "F(pk)"

    @pytest.mark.django_db
    def test_apply_aggregation_and_grouping(self, seed, Post):
        backend = DjangoBackend()
        orm = StrawberryORM.for_django(lazy_resolution="off")
        post_group = orm.group(Post)
        group_field = post_group._field_type
        meta = backend._build_aggregate_types(Post)

        empty_info = SimpleNamespace(selected_fields=[])
        empty_agg = backend.apply_aggregation(Post.objects.all(), empty_info, meta)
        assert empty_agg.count == 0

        count_info = _mock_info_with_aggregates(count=True)
        count_agg = backend.apply_aggregation(Post.objects.all(), count_info, meta)
        assert count_agg.count == 4

        sum_info = _mock_info_with_aggregates(count=True, sum_fields=["id"])
        sum_agg = backend.apply_aggregation(Post.objects.all(), sum_info, meta)
        assert sum_agg.count == 4
        assert sum_agg.sum.id == 10

        empty_groups = backend.apply_grouping(
            Post.objects.all(), post_group(), _mock_info_with_aggregates(), meta
        )
        assert empty_groups == []

        group_info = _mock_info_with_aggregates(path="groups.aggregates", count=True)
        groups = backend.apply_grouping(
            Post.objects.all(),
            post_group(field=group_field(is_published=True)),
            group_info,
            meta,
            order_input=PostOrder(
                field=PostOrder._field_type(is_published=Ordering.DESC)
            ),
        )
        assert len(groups) == 2
        published = next(g for g in groups if g.key.is_published == "True")
        assert published.aggregates.count == 3

    @pytest.mark.django_db
    def test_scope_query_to_group_and_batch_group_items(self, seed, Post):
        backend = DjangoBackend()
        meta = backend._build_aggregate_types(Post)
        key_type = meta.group_key_type

        scoped = backend.scope_query_to_group(
            Post.objects.all(),
            key_type(author_id="1", is_published=None),
        )
        assert scoped.count() == 2

        items_by_key = backend.batch_group_items(
            Post.objects.all(),
            ["author_id"],
            info=None,
            model=Post,
            per_group_limit=1,
            order_input=PostOrder(field=PostOrder._field_type(title=Ordering.ASC)),
        )
        assert len(items_by_key) == 3
        assert all(len(rows) == 1 for rows in items_by_key.values())

        fallback_items = backend.batch_group_items(
            Post.objects.all(),
            ["author_id"],
            info=None,
            model=Post,
            per_group_limit=2,
        )
        assert sum(len(rows) for rows in fallback_items.values()) <= 6

    def test_materialize_query_sync_path(self, seed, Post):
        backend = DjangoBackend()
        qs = Post.objects.all()
        assert len(backend.materialize_query(qs, info=None)) == 4

    def test_count_query(self, seed, Post):
        backend = DjangoBackend()
        assert backend.count_query(Post.objects.all(), info=None) == 4

    @pytest.mark.asyncio
    async def test_materialize_query_async_path(self, seed, Post):
        backend = DjangoBackend()
        qs = Post.objects.all()
        async_result = await await_maybe(backend.materialize_query(qs, info=None))
        assert len(async_result) == 4

    def test_post_process_strawberry_fields(self, User):
        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        processed = orm.backend._post_process_strawberry_fields(UserType)
        assert processed is UserType
        for field in UserType.__strawberry_definition__.fields:
            if getattr(field, "_orm_connection", False):
                continue
            assert field.base_resolver is not None

        plain_backend = DjangoBackend(django_async_safe=False)

        @plain_backend.type(User)
        class PlainUserType:
            id: auto

        assert (
            plain_backend._post_process_strawberry_fields(PlainUserType)
            is PlainUserType
        )

    def test_get_pk_names_without_primary_key(self):
        backend = DjangoBackend()

        class NoPkMeta:
            pk = None

        class NoPkModel:
            _meta = NoPkMeta()

        assert backend._get_pk_names(NoPkModel) == set()

    @pytest.mark.django_db
    def test_apply_aggregation_empty_agg_kwargs(self, seed, Post):
        backend = DjangoBackend()
        meta = backend._build_aggregate_types(Post)
        info = _mock_info_with_aggregates(sum_fields=[])
        result = backend.apply_aggregation(Post.objects.all(), info, meta)
        assert result.count == 0

    @pytest.mark.django_db
    def test_grouping_helpers_skip_empty_field_values(self, seed, Post):
        from strawberry_orm.backends.django import (
            _build_django_order_from_input,
            _extract_django_group_fields,
            _extract_django_overlapping_order,
        )

        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyGroup:
            field: EmptyField | None = strawberry.UNSET

        assert _extract_django_group_fields([EmptyGroup()]) == ([], [])

        @strawberry.input
        class OrderField:
            title: Ordering | None = strawberry.UNSET
            body: Ordering | None = Ordering.ASC

        @strawberry.input
        class OrderEntry:
            field: OrderField | None = strawberry.UNSET

        overlap = _extract_django_overlapping_order(
            OrderEntry(field=OrderField(body=Ordering.ASC)), {"title"}
        )
        assert overlap == []
        order_clauses = _build_django_order_from_input(
            OrderEntry(field=OrderField(body=Ordering.ASC))
        )
        assert len(order_clauses) == 1

    @pytest.mark.django_db
    def test_apply_grouping_returns_empty_without_fields(self, seed, Post):
        backend = DjangoBackend()
        meta = backend._build_aggregate_types(Post)

        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyGroup:
            field: EmptyField | None = strawberry.UNSET

        groups = backend.apply_grouping(
            Post.objects.all(),
            EmptyGroup(),
            _mock_info_with_aggregates(path="groups.aggregates", count=True),
            meta,
        )
        assert groups == []

    @pytest.mark.django_db
    def test_apply_grouping_with_sum_aggregate(self, seed, Post):
        backend = DjangoBackend()
        orm = StrawberryORM.for_django(lazy_resolution="off")
        post_group = orm.group(Post)
        group_field = post_group._field_type
        meta = backend._build_aggregate_types(Post)
        info = _mock_info_with_aggregates(
            path="groups.aggregates", count=True, sum_fields=["id"]
        )
        groups = backend.apply_grouping(
            Post.objects.all(),
            post_group(field=group_field(is_published=True)),
            info,
            meta,
        )
        assert len(groups) >= 1

    def test_django_related_model_non_fk_returns_none(self, Post):
        assert _django_related_model(Post, "title") is None

    def test_build_django_lookup_reference_branch(self):
        q = _build_django_lookup("author_id", ReferenceLookup(exact="1"))
        assert q is not None

    def test_extract_django_group_fields_skips_duplicate(self):
        @strawberry.input
        class DupField:
            title: bool | None = True

        @strawberry.input
        class DupGroup:
            field: DupField | None = strawberry.UNSET

        entry = DupGroup(field=DupField())
        fields, keys = _extract_django_group_fields([entry, entry])
        assert fields.count("title") == 1

    def test_extract_django_overlapping_order_empty_field(self):
        @strawberry.input
        class EmptyOrder:
            field: object | None = strawberry.UNSET

        assert _extract_django_overlapping_order(EmptyOrder(), {"title"}) == []

    def test_build_django_order_from_input_empty_field(self):
        @strawberry.input
        class EmptyField:
            pass

        @strawberry.input
        class EmptyOrder:
            field: EmptyField | None = strawberry.UNSET

        clauses = _build_django_order_from_input(EmptyOrder())
        assert len(clauses) == 1

    @pytest.mark.django_db
    def test_post_process_skips_empty_python_name(self, User):
        backend = DjangoBackend(django_async_safe=True)
        orm = StrawberryORM.for_django(lazy_resolution="off")
        orm._backend = backend

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        empty_field = SimpleNamespace(
            _orm_connection=False,
            base_resolver=None,
            python_name="",
            type=None,
            default_resolver=lambda root, name: getattr(root, name, None),
        )
        UserType.__strawberry_definition__.fields = list(
            UserType.__strawberry_definition__.fields
        ) + [empty_field]
        processed = backend._post_process_strawberry_fields(UserType)
        assert processed is UserType


class TestTraversalModelResolution:
    """Traversal has to follow reverse relations, and give up quietly when a
    name is not a relation at all."""

    def test_no_model_means_no_relation(self):
        from strawberry_orm.backends.django import _django_traversal_model

        assert _django_traversal_model(None, "author") is None

    def test_unknown_field_is_not_a_relation(self, User):
        from strawberry_orm.backends.django import _django_traversal_model

        assert _django_traversal_model(User, "not_a_field") is None

    def test_reverse_relation_resolves(self, Post, User):
        from strawberry_orm.backends.django import (
            _django_related_model,
            _django_traversal_model,
        )

        assert _django_traversal_model(User, "posts") is Post
        assert _django_related_model(User, "posts") is None


class TestScopedRelationHelpers:
    """Branches of the resolve-time scoping helpers that the schema tests miss."""

    def test_a_null_to_one_relation_stays_null(self):
        """A nullable relation with nothing on the other end needs no query."""
        from strawberry_orm.backends.django import _scoped_related_instance

        class Holder:
            author = None

        class ScopingBackend:
            @staticmethod
            def relation_scope(model, field_name, info):
                return lambda queryset, info: queryset

        assert (
            _scoped_related_instance(ScopingBackend(), Holder(), "author", None) is None
        )

    def test_repeated_lookups_are_collapsed(self):
        """Two aliases of one relation must not become two prefetches.

        Django rejects a repeated path when either occurrence carries a
        queryset, so the same field selected twice would raise.
        """
        from django.db.models import Prefetch

        from strawberry_orm.backends.django import _dedupe_lookups

        first = Prefetch("posts")
        collapsed = _dedupe_lookups(["author", "posts", "author", first, "posts"])

        assert collapsed == ["author", "posts"]

    def test_a_scoped_lookup_outranks_a_bare_one(self, Post):
        """The scope lives on the ``Prefetch``, so it must survive deduping.

        A relation can be reached both by name - from ``using=`` - and as a
        scoped prefetch. Keeping the bare path would drop the scope and load
        rows the caller may not read.
        """
        from django.db.models import Prefetch

        from strawberry_orm.backends.django import _dedupe_lookups

        scoped = Prefetch("posts", queryset=Post.objects.filter(is_published=True))

        assert _dedupe_lookups(["posts", scoped]) == [scoped]
        assert _dedupe_lookups([scoped, "posts"]) == [scoped]

    def test_the_first_scoped_lookup_wins_over_a_later_one(self, Post):
        from django.db.models import Prefetch

        from strawberry_orm.backends.django import _dedupe_lookups

        first = Prefetch("posts", queryset=Post.objects.filter(is_published=True))
        second = Prefetch("posts", queryset=Post.objects.all())

        assert _dedupe_lookups([first, second]) == [first]

    def test_lookups_without_a_path_are_kept(self):
        from strawberry_orm.backends.django import _dedupe_lookups

        opaque = object()
        assert _dedupe_lookups([opaque, opaque]) == [opaque, opaque]
