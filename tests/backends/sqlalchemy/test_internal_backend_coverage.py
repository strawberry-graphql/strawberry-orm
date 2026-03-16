"""Focused backend-adapter coverage for exact helper branches."""

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import text

from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _build_sa_field_clause,
    _build_sa_filter,
    _build_lookup_clauses,
    _build_sa_ordering,
)
from strawberry_orm import Ordering


@strawberry.input
class EmptyFilterGroup:
    all: list["EmptyFilterGroup"] | None = strawberry.UNSET
    any: list["EmptyFilterGroup"] | None = strawberry.UNSET
    one_of: list["EmptyFilterGroup"] | None = strawberry.UNSET
    not_: "EmptyFilterGroup | None" = strawberry.UNSET
    field: object | None = strawberry.UNSET


@strawberry.input
class InvalidFieldInput:
    missing: object | None = strawberry.UNSET


@strawberry.input
class RegexLookup:
    regex: str | None = strawberry.UNSET
    i_regex: str | None = strawberry.UNSET


@strawberry.input
class InvalidOrderInput:
    missing: Ordering | None = strawberry.UNSET


class TestInternalBackendCoverage:
    def test_filter_helpers_handle_none_and_empty_groups(self, User):
        assert _build_sa_filter(None, User) is None
        assert _build_sa_filter(EmptyFilterGroup(all=[]), User) is None
        assert _build_sa_filter(EmptyFilterGroup(any=[]), User) is None
        assert _build_sa_filter(EmptyFilterGroup(one_of=[]), User) is None
        assert _build_sa_field_clause(InvalidFieldInput(), User) is None

        with pytest.raises(ValueError, match="maximum is 0"):
            _build_sa_filter(
                EmptyFilterGroup(all=[EmptyFilterGroup()]), User, max_branches=0
            )
        with pytest.raises(ValueError, match="maximum is 0"):
            _build_sa_filter(
                EmptyFilterGroup(one_of=[EmptyFilterGroup()]),
                User,
                max_branches=0,
            )
        with pytest.raises(ValueError, match="Regex filters are disabled"):
            _build_lookup_clauses(
                User.name, RegexLookup(regex="a.*"), enable_regex=False
            )
        with pytest.raises(ValueError, match="Regex filters are disabled"):
            _build_lookup_clauses(
                User.name,
                RegexLookup(i_regex="a.*"),
                enable_regex=False,
            )
        assert _build_sa_ordering(InvalidOrderInput(), User) == []

    def test_query_object_helpers_handle_fallback_values(self, sa_session):
        backend = SQLAlchemyBackend(dialect="sqlite")
        plain = ["a", "b"]
        assert backend.apply_optimizer_hints(None, plain, info=None) == plain

        class Info:
            context = {"session": sa_session}

        with pytest.raises(ValueError, match="Invalid filter expression"):
            backend._execute_stmt_sync(sa_session, text("select * from missing_table"))

    def test_internal_queryset_hooks_and_ordering_helpers(self, User):
        backend = SQLAlchemyBackend(dialect="sqlite")
        backend._type_registry["UserType"] = User

        class QueryType:
            @classmethod
            def get_queryset(cls, stmt, info):
                return stmt.where(User.name == "Alice")

        backend._type_querysets[User] = QueryType.get_queryset
        backend._store.hints = {
            "UserType": {
                "name": SimpleNamespace(
                    load=lambda stmt: stmt.where(User.email.like("%example.com")),
                    only=None,
                    disable_optimization=False,
                )
            }
        }

        stmt = backend.get_default_queryset(User)
        scoped = backend._apply_nested_queryset(stmt, User, "name", User, info=None)
        text_value = str(scoped)
        assert '"user".name' in text_value
        assert '"user".email' in text_value
