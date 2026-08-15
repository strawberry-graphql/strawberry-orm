"""Tests for custom filter_type / order_type with @filter_field / @order_field."""

import pytest
import strawberry
from tortoise.queryset import Q

from strawberry_orm import StrawberryORM, filter_field, order_field
from strawberry_orm.types import Ordering, auto
from tests.backends.tortoise.models import User as TortUser


class TestCustomFilterType:
    def _build_schema(self):
        orm = StrawberryORM.for_tortoise()

        @orm.filter_type(TortUser)
        class UserFilter:
            name: auto
            email: auto

            @filter_field
            def search(self, value: str, query):
                return query.filter(
                    Q(name__icontains=value) | Q(email__icontains=value)
                )

            @filter_field
            def has_posts(self, value: bool, query):
                if value:
                    return query.filter(posts__id__not_isnull=True).distinct()
                return query.filter(posts__id__isnull=True)

        @orm.type(TortUser, filters=UserFilter)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field.auto()
            async def users(self) -> list[UserType]:
                return orm.get_default_queryset(TortUser)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    async def test_custom_search_filter(self, seed):
        schema = self._build_schema()
        result = await schema.execute('{ users(filter: { search: "alice" }) { name } }')
        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}

    async def test_custom_search_filter_matches_email(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            '{ users(filter: { search: "example.com" }) { name } }'
        )
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert names == {"Alice", "Bob"}

    async def test_custom_has_posts_filter(self, seed):
        schema = self._build_schema()
        result = await schema.execute("{ users(filter: { hasPosts: true }) { name } }")
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert "Alice" in names
        assert "Bob" in names

    async def test_custom_filter_combined_with_standard_via_all(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            """
            {
                users(filter: { all: [
                    { search: "example.com" },
                    { field: { name: { exact: "Alice" } } }
                ] }) { name email }
            }
            """
        )
        assert result.errors is None
        assert result.data == {
            "users": [{"name": "Alice", "email": "alice@example.com"}]
        }

    async def test_auto_fields_still_work(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            '{ users(filter: { field: { name: { exact: "Bob" } } }) { name } }'
        )
        assert result.errors is None
        assert result.data == {"users": [{"name": "Bob"}]}


class TestCustomOrderType:
    def _build_schema(self):
        orm = StrawberryORM.for_tortoise()
        UserFilter = orm.filter(TortUser)

        @orm.order_type(TortUser)
        class UserOrder:
            name: auto

            @order_field
            def post_count(self, value: Ordering, query):
                from tortoise.functions import Count

                query = query.annotate(_post_count=Count("posts"))
                dir_value = value.value if hasattr(value, "value") else str(value)
                if dir_value.startswith("DESC"):
                    return query.order_by("-_post_count")
                return query.order_by("_post_count")

        @orm.type(TortUser, filters=UserFilter, order=UserOrder)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field.auto()
            async def users(self) -> list[UserType]:
                return orm.get_default_queryset(TortUser)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    async def test_custom_order_post_count_asc(self, seed):
        schema = self._build_schema()
        result = await schema.execute("{ users(order: [{ postCount: ASC }]) { name } }")
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[-1] == "Alice"

    async def test_custom_order_post_count_desc(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            "{ users(order: [{ postCount: DESC }]) { name } }"
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[0] == "Alice"

    async def test_auto_order_fields_still_work(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            "{ users(order: [{ field: { name: DESC } }]) { name } }"
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names == ["Charlie", "Bob", "Alice"]

    async def test_custom_and_standard_order_combined(self, seed):
        schema = self._build_schema()
        result = await schema.execute(
            """
            { users(order: [
                { postCount: DESC },
                { field: { name: ASC } }
            ]) { name } }
            """
        )
        assert result.errors is None
        assert result.data["users"] is not None


class TestFilterFieldValueAnnotationValidation:
    def test_missing_value_annotation_raises(self):
        orm = StrawberryORM.for_tortoise()

        with pytest.raises(TypeError, match="'value' parameter"):

            @orm.filter_type(TortUser)
            class BadFilter:
                @filter_field
                def broken(self, value, query):
                    return query
