"""The SQLAlchemy filter helpers refuse regex unless the caller allows it.

The backend passes ``enable_regex_filters`` on, which is off by default; a
helper called without it must not be the one place a regex reaches the
database, where a pattern can run for as long as the database lets it.
"""

import inspect
from dataclasses import dataclass
from typing import Any

import pytest
import strawberry

from strawberry_orm.backends.sqlalchemy import (
    _build_lookup_clauses,
    _build_sa_field_clause,
    _build_sa_filter,
    _build_sa_reference_field_clause,
)
from strawberry_orm.filters import StringLookup


@dataclass
class NameField:
    name: Any = strawberry.UNSET


@dataclass
class NameFilter:
    field: Any = strawberry.UNSET


@pytest.mark.parametrize(
    "helper",
    [
        _build_sa_filter,
        _build_sa_field_clause,
        _build_sa_reference_field_clause,
        _build_lookup_clauses,
    ],
    ids=lambda helper: helper.__name__,
)
def test_a_filter_helper_leaves_regex_off_by_default(helper):
    assert inspect.signature(helper).parameters["enable_regex"].default is False


@pytest.mark.parametrize("lookup", ["regex", "i_regex"])
def test_a_lookup_refuses_a_regex_by_default(User, lookup):
    with pytest.raises(ValueError, match="Regex filters are disabled"):
        _build_lookup_clauses(User.name, StringLookup(**{lookup: "a.*"}))


def test_a_field_refuses_a_regex_by_default(User):
    with pytest.raises(ValueError, match="Regex filters are disabled"):
        _build_sa_field_clause(NameField(name=StringLookup(regex="a.*")), User)


def test_a_filter_refuses_a_regex_by_default(User):
    filter_input = NameFilter(field=NameField(name=StringLookup(regex="a.*")))
    with pytest.raises(ValueError, match="Regex filters are disabled"):
        _build_sa_filter(filter_input, User)


def test_a_regex_is_built_when_allowed(User):
    clauses = _build_lookup_clauses(
        User.name, StringLookup(regex="a.*"), enable_regex=True
    )
    assert len(clauses) == 1
