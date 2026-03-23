"""Tests for custom filter_type / order_type with @filter_field / @order_field."""

import pytest
import strawberry
from django.db.models import Count, Q

from strawberry_orm import StrawberryORM, filter_field, order_field
from strawberry_orm.types import Ordering, auto
from tests.backends.django.models import User as DjUser


class TestCustomFilterType:
    def _build_schema(self):
        orm = StrawberryORM("django")

        @orm.filter_type(DjUser)
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
                q = query.annotate(_post_count=Count("posts"))
                if value:
                    return q.filter(_post_count__gt=0)
                return q.filter(_post_count=0)

        @orm.type(DjUser, filters=UserFilter)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(DjUser)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_custom_search_filter(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync('{ users(filter: { search: "alice" }) { name } }')
        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}

    def test_custom_search_filter_matches_email(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync(
            '{ users(filter: { search: "example.com" }) { name } }'
        )
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert names == {"Alice", "Bob"}

    def test_custom_has_posts_filter(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync("{ users(filter: { hasPosts: true }) { name } }")
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert "Alice" in names
        assert "Bob" in names

    def test_custom_filter_combined_with_standard_via_all(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync(
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

    def test_auto_fields_still_work(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync(
            '{ users(filter: { field: { name: { exact: "Bob" } } }) { name } }'
        )
        assert result.errors is None
        assert result.data == {"users": [{"name": "Bob"}]}


class TestCustomOrderType:
    def _build_schema(self):
        orm = StrawberryORM("django")
        UserFilter = orm.filter(DjUser)

        @orm.order_type(DjUser)
        class UserOrder:
            name: auto

            @order_field
            def post_count(self, value: Ordering, query):
                from django.db.models import F

                query = query.annotate(_post_count=Count("posts"))
                dir_value = value.value if hasattr(value, "value") else str(value)
                nulls_first = "NULLS_FIRST" in dir_value
                nulls_last = "NULLS_LAST" in dir_value
                if dir_value.startswith("DESC"):
                    expr = F("_post_count").desc(
                        nulls_first=nulls_first or None,
                        nulls_last=nulls_last or None,
                    )
                else:
                    expr = F("_post_count").asc(
                        nulls_first=nulls_first or None,
                        nulls_last=nulls_last or None,
                    )
                return query.order_by(expr)

        @orm.type(DjUser, filters=UserFilter, order=UserOrder)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(DjUser)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_custom_order_post_count_asc(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync("{ users(order: [{ postCount: ASC }]) { name } }")
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[-1] == "Alice"

    def test_custom_order_post_count_desc(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync("{ users(order: [{ postCount: DESC }]) { name } }")
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[0] == "Alice"

    def test_auto_order_fields_still_work(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync(
            "{ users(order: [{ field: { name: DESC } }]) { name } }"
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names == ["Charlie", "Bob", "Alice"]

    def test_custom_and_standard_order_combined(self, seed):
        schema = self._build_schema()
        result = schema.execute_sync(
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
        orm = StrawberryORM("django")

        with pytest.raises(TypeError, match="'value' parameter"):

            @orm.filter_type(DjUser)
            class BadFilter:
                @filter_field
                def broken(self, value, query):
                    return query
