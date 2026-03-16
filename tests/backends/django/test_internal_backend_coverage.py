"""Focused backend-adapter coverage for exact helper branches."""

import pytest
import strawberry

from types import SimpleNamespace

from strawberry_orm.backends.django import (
    DjangoBackend,
    _build_django_field_clause,
    _build_django_filter,
)


@strawberry.input
class EmptyFilterGroup:
    all: list["EmptyFilterGroup"] | None = strawberry.UNSET
    any: list["EmptyFilterGroup"] | None = strawberry.UNSET
    one_of: list["EmptyFilterGroup"] | None = strawberry.UNSET
    not_: "EmptyFilterGroup | None" = strawberry.UNSET
    field: object | None = strawberry.UNSET


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self):
        assert _build_django_filter(None) is None
        assert _build_django_filter(EmptyFilterGroup(all=[])) is None
        assert _build_django_filter(EmptyFilterGroup(any=[])) is None
        assert _build_django_filter(EmptyFilterGroup(one_of=[])) is None

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
        assert _build_django_filter(EmptyFilterGroup()) is None
        assert str(_build_django_field_clause(EmptyFilterGroup())) == "(AND: )"
