"""The costly-lookup budget adds up across one request's filters.

Under the library's schema extensions an execution is open, and the regex
lookups and text-lookup characters of every filter it applies are added up,
one entry for each place a field is selected: a relation field resolved for
many parent rows applies its filter each time, but it is one filter of the
request. A filter that is refused adds nothing. Outside an execution, or
with no ``info``, each filter is held to the budget alone.
"""

import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import strawberry
from graphql.pyutils import Path

from strawberry_orm.backends.filter_budget import check_filter_budget
from strawberry_orm.filters import StringLookup
from strawberry_orm.lazy_resolution import execution_scope

UNSET = strawberry.UNSET
LIMITS = {
    "max_depth": 10,
    "max_branches": 50,
    "max_values": 500,
    "max_regex_filters": 2,
    "max_text_length": 8,
}


@dataclass
class Field:
    name: Any = UNSET
    email: Any = UNSET


@dataclass
class Filter:
    field: Any = UNSET
    any: Any = UNSET
    all: Any = UNSET
    one_of: Any = UNSET
    not_: Any = UNSET
    object: Any = UNSET


@dataclass
class Relations:
    posts: Any = UNSET
    author: Any = UNSET


def regex(count=1, **lookup):
    branches = [Filter(field=Field(name=StringLookup(regex="^a", **lookup)))] * count
    return Filter(any=branches)


def contains(text):
    return Filter(field=Field(name=StringLookup(contains=text)))


def info(*keys, node=None):
    path = None
    for key in keys:
        path = Path(path, key, None)
    return SimpleNamespace(
        path=path, field_nodes=[node or object()], parent_type=SimpleNamespace(name="Q")
    )


def check(filter_input, at=None):
    check_filter_budget(filter_input, info=at, **LIMITS)


class TestOneFilter:
    @pytest.mark.parametrize(
        "lookup", [{"regex": "^a"}, {"i_regex": "^a"}, {"regex": "^a", "i_regex": "^b"}]
    )
    def test_each_regex_and_iregex_counts(self, lookup):
        one = Filter(
            field=Field(name=StringLookup(**lookup), email=StringLookup(**lookup))
        )
        if len(lookup) * 2 > LIMITS["max_regex_filters"]:
            with pytest.raises(ValueError, match="over 2 regex lookups"):
                check(one)
        else:
            check(one)

    def test_through_not_and_object(self):
        nested = Filter(
            not_=Filter(object=Relations(posts=regex(), author=Filter(not_=regex())))
        )
        check(nested)
        with pytest.raises(ValueError, match="over 2 regex lookups"):
            check(Filter(all=[nested, regex()]))

    def test_text_counts_by_its_characters(self):
        check(contains("a" * 8))
        with pytest.raises(ValueError, match="over 8 characters"):
            check(contains("a" * 9))

    @pytest.mark.parametrize(
        "op", ["i_contains", "starts_with", "i_starts_with", "ends_with", "i_ends_with"]
    )
    def test_every_text_lookup_counts(self, op):
        five = StringLookup(**{op: "a" * 5})
        with pytest.raises(ValueError, match="over 8 characters"):
            check(Filter(field=Field(name=five, email=five)))

    def test_a_list_of_text_is_left_to_the_value_budget(self):
        listed = StringLookup(in_list=["a" * 100] * 3)
        check(Filter(field=Field(name=listed)))


class TestOneRequest:
    def test_filters_at_different_places_add_up(self):
        with execution_scope():
            check(regex(), info("a"))
            check(regex(), info("b"))
            with pytest.raises(ValueError, match="request's filters"):
                check(regex(), info("c"))

    def test_text_adds_up_too(self):
        with execution_scope():
            check(contains("a" * 5), info("a"))
            with pytest.raises(ValueError, match="request's filters"):
                check(contains("a" * 5), info("b"))

    def test_one_place_resolved_for_many_parents_counts_once(self):
        node = object()
        with execution_scope():
            for parent in range(10):
                check(regex(2), info("users", parent, "posts", node=node))

    def test_the_same_path_selected_twice_counts_twice(self):
        # Kept alive, as a document's nodes are, so no two share an id().
        nodes = [object(), object(), object()]
        with execution_scope():
            check(regex(), info("users", node=nodes[0]))
            check(regex(), info("users", node=nodes[1]))
            with pytest.raises(ValueError, match="request's filters"):
                check(regex(), info("users", node=nodes[2]))

    def test_a_refused_filter_adds_nothing(self):
        with execution_scope():
            check(regex(), info("a"))
            with pytest.raises(ValueError):
                check(regex(2), info("b"))
            check(regex(), info("c"))

    def test_values_do_not_use_up_the_regex_budget(self):
        with execution_scope():
            for key in range(5):
                check(Filter(field=Field(name=StringLookup(exact="a"))), info(str(key)))
            check(regex(2), info("x"))

    def test_the_next_request_starts_again(self):
        for _ in range(3):
            with execution_scope():
                check(regex(2), info("a"))

    def test_without_an_execution_each_filter_stands_alone(self):
        for key in "abc":
            check(regex(2), info(key))

    def test_without_info_each_filter_stands_alone(self):
        with execution_scope():
            for _ in range(3):
                check(regex(2))


@dataclass
class Searchable(Filter):
    search: Any = UNSET


class TestValueSize:
    """Every value is measured where it is written, whatever its kind."""

    @staticmethod
    def check(filter_input, at=None):
        check_filter_budget(filter_input, info=at, **LIMITS, max_value_length=100)

    def test_each_reference_counts(self):
        self.check(Filter(field=Field(name=StringLookup(in_list=["a" * 45] * 2))))
        with pytest.raises(ValueError, match="over 100 characters of values"):
            self.check(Filter(field=Field(name=StringLookup(in_list=["a" * 45] * 3))))

    def test_a_custom_filter_field_counts(self):
        self.check(Searchable(search="a" * 100))
        with pytest.raises(ValueError, match="over 100 characters of values"):
            self.check(Searchable(search="a" * 101))

    @pytest.mark.parametrize(
        "value",
        [
            b"x" * 101,
            Decimal("1" * 101),
            10**400,
            {"key": "v" * 101},
            ["v" * 51, "v" * 51, True, 1.5, None],
            Field(name="v" * 101),
            [uuid.UUID(int=n) for n in range(3)],
        ],
        ids=["bytes", "decimal", "int", "dict", "list", "input", "uuids"],
    )
    def test_every_kind_of_value_is_measured(self, value):
        with pytest.raises(ValueError, match="over 100 characters of values"):
            self.check(Searchable(search=value))

    def test_a_value_nested_deeper_than_the_stack_is_measured(self):
        deep: Any = "v"
        for _ in range(sys.getrecursionlimit() * 2):
            deep = [deep]
        with pytest.raises(ValueError, match="over 100 characters of values"):
            self.check(Searchable(search=deep))

    def test_values_add_up_across_a_request(self):
        with execution_scope():
            self.check(Searchable(search="a" * 60), info("a"))
            with pytest.raises(ValueError, match="request's filters have over 100"):
                self.check(Searchable(search="a" * 60), info("b"))
