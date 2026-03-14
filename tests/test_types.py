"""Tests for shared types: Ordering, lookups, OperationInfo."""

from __future__ import annotations

from strawberry_orm import (
    BooleanLookup,
    FloatComparisonLookup,
    IntComparisonLookup,
    Ordering,
    StringLookup,
)


class TestOrdering:
    def test_enum_values(self):
        assert Ordering.ASC.value == "ASC"
        assert Ordering.DESC.value == "DESC"
        assert Ordering.ASC_NULLS_FIRST.value == "ASC_NULLS_FIRST"
        assert Ordering.ASC_NULLS_LAST.value == "ASC_NULLS_LAST"
        assert Ordering.DESC_NULLS_FIRST.value == "DESC_NULLS_FIRST"
        assert Ordering.DESC_NULLS_LAST.value == "DESC_NULLS_LAST"

    def test_all_values_present(self):
        assert len(Ordering) == 6


class TestLookupTypes:
    def test_string_lookup_fields(self):
        lookup = StringLookup()
        assert hasattr(lookup, "exact")
        assert hasattr(lookup, "contains")
        assert hasattr(lookup, "i_contains")
        assert hasattr(lookup, "starts_with")
        assert hasattr(lookup, "regex")

    def test_int_comparison_lookup_fields(self):
        lookup = IntComparisonLookup()
        assert hasattr(lookup, "exact")
        assert hasattr(lookup, "gt")
        assert hasattr(lookup, "gte")
        assert hasattr(lookup, "lt")
        assert hasattr(lookup, "lte")
        assert hasattr(lookup, "range")

    def test_boolean_lookup_fields(self):
        lookup = BooleanLookup()
        assert hasattr(lookup, "exact")
        assert hasattr(lookup, "is_null")
