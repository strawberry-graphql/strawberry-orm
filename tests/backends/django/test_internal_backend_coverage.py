"""Focused backend-adapter coverage for exact helper branches."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

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
    _build_django_reference_field_clause,
    _build_django_reference_lookup,
    _coerce_reference_value,
    _django_forward_fk_attname,
    _django_related_model,
    _primary_key,
)
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
    filter_tree_uses_only_reference_lookups,
)
from strawberry_orm.filters import IntComparisonLookup, ReferenceLookup, StringLookup
from strawberry_orm.grouping import GroupPath, group_paths
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

        group_info = _mock_info_with_aggregates(path="groups.aggregates", count=True)
        groups = backend.apply_grouping(
            Post.objects.all(),
            group_paths(
                backend, Post, post_group(field=group_field(is_published=True))
            ),
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
    def test_batch_group_items(self, seed, Post):
        backend = DjangoBackend()
        by_author = [GroupPath(alias="_g0", name="author_id", column="author_id")]

        items_by_key = backend.batch_group_items(
            Post.objects.all(),
            by_author,
            info=None,
            model=Post,
            per_group_limit=1,
            order_input=PostOrder(field=PostOrder._field_type(title=Ordering.ASC)),
        )
        assert len(items_by_key) == 3
        assert all(len(rows) == 1 for rows in items_by_key.values())

        fallback_items = backend.batch_group_items(
            Post.objects.all(),
            by_author,
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

    def test_a_page_is_ordered_to_the_row(self):
        """The cut ends its order with the key, and never names the key twice."""
        from django.db.models import F

        from strawberry_orm.backends.django import _ordered_to_the_row, _pk_tiebreak

        assert _pk_tiebreak(["email"], User) == "id"
        assert _pk_tiebreak(["-pk"], User) is None
        assert _pk_tiebreak([F("id").desc()], User) is None
        assert _ordered_to_the_row(User.objects.order_by("email")).query.order_by == (
            "email",
            "id",
        )
        # A slice cannot be reordered, and reordering would drop an
        # .extra(order_by=), which outranks every other order.
        capped = _ordered_to_the_row(User.objects.order_by("email")[:5])
        assert capped.query.order_by == ("email",)
        extra = _ordered_to_the_row(User.objects.extra(order_by=["-name"]))
        assert list(extra.query.extra_order_by) == ["-name"]
        assert extra.query.order_by == ()

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
            group_paths(
                backend, Post, post_group(field=group_field(is_published=True))
            ),
            info,
            meta,
        )
        assert len(groups) >= 1

    def test_django_related_model_non_fk_returns_none(self, Post):
        assert _django_related_model(Post, "title") is None

    def test_build_django_lookup_reference_branch(self):
        q = _build_django_lookup("author_id", ReferenceLookup(exact="1"))
        assert q is not None

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

    def test_scoped_prefetch_helpers_cover_cache_shapes(self, seed, User, Post):
        from strawberry_orm.backends.django import (
            _clear_django_relation_cache,
            _clear_scoped_relation_caches,
            _django_related_children,
            _mark_scoped_prefetches,
            _scoped_prefetch,
            _scoped_related_instance,
            _scoped_related_queryset,
            _strip_conflicting_select_related,
        )
        from strawberry_orm.lazy_resolution import mark_relations_scoped

        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class AuthorType:
            name: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(name="Alice")

        @orm.type(Post)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

        info = SimpleNamespace()
        author_prefetch = _scoped_prefetch("author", User.objects.all())
        everything = _strip_conflicting_select_related(
            Post.objects.select_related(), [author_prefetch]
        )
        assert not everything.query.select_related

        narrowed = _strip_conflicting_select_related(
            Post.objects.select_related("author"), [author_prefetch]
        )
        assert not narrowed.query.select_related

        post = Post.objects.select_related("author").get(title="Hello World")
        _clear_scoped_relation_caches([post], [author_prefetch])
        assert not Post._meta.get_field("author").is_cached(post)

        post.author = User(name="Ghost", email="ghost@example.com")
        assert _scoped_related_instance(orm._backend, post, "author", info) is None

        bob = User.objects.prefetch_related("posts").get(name="Bob")
        scoped_posts = _scoped_related_queryset(orm._backend, bob, "posts", info)
        assert list(scoped_posts) == []

        alice = User.objects.get(name="Alice")
        alice_post = Post.objects.get(title="Hello World")
        alice_post.author = alice
        nested = _scoped_prefetch("author__posts", Post.objects.all())
        _mark_scoped_prefetches([alice_post], [nested])

        class Bare:
            pass

        empty = Bare()
        empty._strawberry_orm_scoped = True
        empty.prefetch_through = ""
        empty.to_attr = None
        _mark_scoped_prefetches([alice_post], [empty])

        holder = Bare()
        holder._state = SimpleNamespace(fields_cache={"author": alice})
        assert _django_related_children([holder], "author") == [alice]

        listed = Bare()
        listed._state = SimpleNamespace(fields_cache={})
        listed._prefetched_objects_cache = {"posts": [alice_post]}
        assert _django_related_children([listed], "posts") == [alice_post]

        cached = Post.objects.filter(author=alice)
        list(cached)
        via_dict = Bare()
        via_dict._state = SimpleNamespace(fields_cache={})
        via_dict.__dict__["posts"] = cached
        assert alice_post in _django_related_children([via_dict], "posts")

        missing = Bare()
        missing._state = SimpleNamespace(fields_cache={})
        assert _django_related_children([missing], "nope") == []

        _clear_django_relation_cache(alice, "not_a_field")
        mark_relations_scoped([post], ["author"])
        post.author = alice
        assert _scoped_related_instance(orm._backend, post, "author", info) is alice

    def test_an_already_checked_row_is_not_filtered_out(self, seed, Post):
        from strawberry_orm.lazy_resolution import mark_rows_scoped

        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(Post)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

        published = Post.objects.get(title="Hello World")
        draft = Post.objects.get(title="Draft Post")
        mark_rows_scoped([draft])
        kept = orm._backend.scope_instances([published, draft], SimpleNamespace())
        assert kept == [published, draft]

    def test_a_cached_null_to_one_reads_as_absent(self, seed, Comment):
        from strawberry_orm.backends.django import _scoped_related_instance

        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(Comment)
        class CommentType:
            body: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.exclude(body="unreachable")

        comment = Comment.objects.select_related("parent").filter(parent=None).first()
        assert comment is not None
        assert (
            _scoped_related_instance(orm._backend, comment, "parent", SimpleNamespace())
            is None
        )


@dataclass
class _Arm:
    field: Any = strawberry.UNSET
    object: Any = strawberry.UNSET


@dataclass
class _Relations:
    author: Any = strawberry.UNSET
    posts: Any = strawberry.UNSET
    tags: Any = strawberry.UNSET


@dataclass
class _Columns:
    id: Any = strawberry.UNSET


@dataclass
class _ByCustom:
    by: Any = strawberry.UNSET


_ByCustom._custom_groups = {"by": lambda self, value, query: "id"}


@dataclass
class _Filter:
    all: Any = strawberry.UNSET
    not_: Any = strawberry.UNSET
    object: Any = strawberry.UNSET


class TestGroupingHelpers:
    def test_an_unset_entry_groups_by_nothing(self, Post):
        assert group_paths(DjangoBackend(), Post, [None]) == []

    def test_a_to_many_relation_is_only_grouped_by_directly(self, Post):
        posts = _Arm(object=_Relations(posts=_Arm(field=_Columns(id=True))))
        with pytest.raises(ValueError, match="only be grouped by directly"):
            group_paths(DjangoBackend(), Post, _Arm(object=_Relations(author=posts)))

    def test_a_group_field_is_not_reached_through_a_relation(self, Post):
        through = _Arm(object=_Relations(author=_ByCustom(by=True)))
        with pytest.raises(ValueError, match="cannot be reached through object"):
            group_paths(DjangoBackend(), Post, through)

    def test_a_filter_crosses_to_many_inside_any_branch(self, Post):
        backend = DjangoBackend()
        tags = _Filter(object=_Relations(tags=_Filter()))
        assert backend.filter_crosses_to_many(tags, None) is False
        assert backend.filter_crosses_to_many(_Filter(all=[tags]), Post) is True
        assert backend.filter_crosses_to_many(_Filter(not_=tags), Post) is True
        author = _Filter(object=_Relations(author=_Filter()))
        assert backend.filter_crosses_to_many(author, Post) is False

    def test_the_object_arm_leaves_out_excluded_and_sensitive_relations(
        self, Post, User
    ):
        backend = DjangoBackend()
        backend.group(User)
        fields_meta = [
            ("author", None, True, User),
            ("secret_owner", None, True, User),
        ]
        assert backend._group_object_arm(Post, fields_meta, None, {"author"}) == (
            None,
            {},
        )

    def test_only_single_valued_names_rank_groups(self, Post):
        from django.db.models import F
        from django.db.models.expressions import OrderBy

        from strawberry_orm.backends.django import _connection_ordering, _single_valued

        assert _single_valued(Post, "pk")
        assert _single_valued(Post, "author")
        assert not _single_valued(Post, "nothing")
        query = Post.objects.order_by("?", OrderBy(F("title")), F("id"))
        assert [str(clause) for clause in _connection_ordering(query, None)] == [
            str(OrderBy(F("title"))),
            str(F("id").asc()),
        ]
