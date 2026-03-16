"""Focused backend-adapter coverage for exact helper branches."""

from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm.backends.tortoise import (
    TortoiseBackend,
    _CustomRel,
    _apply_python_ordering,
    _build_tortoise_filter,
    _build_tortoise_lookup,
    _get_reverse_fk_field,
)


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
        assert _build_tortoise_filter(None) is None
        assert _build_tortoise_filter(EmptyFilterGroup()) is None
        assert _build_tortoise_filter(EmptyFilterGroup(all=[])) is None
        assert _build_tortoise_filter(EmptyFilterGroup(any=[])) is None
        assert _build_tortoise_filter(EmptyFilterGroup(one_of=[])) is None

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
