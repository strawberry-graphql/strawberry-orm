"""Tests for custom filter_type / order_type with @filter_field / @order_field."""

import pytest
import strawberry
from sqlalchemy import func, or_, select

from strawberry_orm import StrawberryORM, filter_field, order_field
from strawberry_orm.types import Ordering, auto


class TestCustomFilterType:
    def _build_schema(self, User, Post):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

        @orm.filter_type(User)
        class UserFilter:
            name: auto
            email: auto

            @filter_field
            def search(self, value: str, query):
                return query.where(
                    or_(
                        User.name.ilike(f"%{value}%"),
                        User.email.ilike(f"%{value}%"),
                    )
                )

            @filter_field
            def has_posts(self, value: bool, query):
                subq = (
                    select(func.count(Post.id))
                    .where(Post.author_id == User.id)
                    .correlate(User)
                    .scalar_subquery()
                )
                if value:
                    return query.where(subq > 0)
                return query.where(subq == 0)

        @orm.type(User, filters=UserFilter)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(User)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_custom_search_filter(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            '{ users(filter: { search: "alice" }) { name } }',
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}

    def test_custom_search_filter_matches_email(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            '{ users(filter: { search: "example.com" }) { name } }',
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert names == {"Alice", "Bob"}

    def test_custom_has_posts_filter(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            "{ users(filter: { hasPosts: true }) { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = {u["name"] for u in result.data["users"]}
        assert "Alice" in names
        assert "Bob" in names

    def test_custom_filter_combined_with_standard_via_all(
        self, sa_session, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                users(filter: { all: [
                    { search: "example.com" },
                    { field: { name: { exact: "Alice" } } }
                ] }) { name email }
            }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [{"name": "Alice", "email": "alice@example.com"}]
        }

    def test_auto_fields_still_work(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            '{ users(filter: { field: { name: { exact: "Bob" } } }) { name } }',
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {"users": [{"name": "Bob"}]}


class TestCustomOrderType:
    def _build_schema(self, User, Post):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        UserFilter = orm.filter(User)

        @orm.order_type(User)
        class UserOrder:
            name: auto

            @order_field
            def post_count(self, value: Ordering, query):
                query = query.outerjoin(Post, Post.author_id == User.id).group_by(
                    User.id
                )
                col = func.count(Post.id)
                dir_value = value.value if hasattr(value, "value") else str(value)
                if dir_value.startswith("DESC"):
                    return query.order_by(col.desc())
                return query.order_by(col.asc())

        @orm.type(User, filters=UserFilter, order=UserOrder)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserType]:
                return orm.get_default_queryset(User)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_custom_order_post_count_asc(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            "{ users(order: [{ postCount: ASC }]) { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[-1] == "Alice"

    def test_custom_order_post_count_desc(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            "{ users(order: [{ postCount: DESC }]) { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names[0] == "Alice"

    def test_auto_order_fields_still_work(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            "{ users(order: [{ field: { name: DESC } }]) { name } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        names = [u["name"] for u in result.data["users"]]
        assert names == ["Charlie", "Bob", "Alice"]

    def test_custom_and_standard_order_combined(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            { users(order: [
                { postCount: DESC },
                { field: { name: ASC } }
            ]) { name } }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data["users"] is not None


class TestFilterFieldValueAnnotationValidation:
    def test_missing_value_annotation_raises(self):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        from tests.backends.sqlalchemy.models import User

        with pytest.raises(TypeError, match="'value' parameter"):

            @orm.filter_type(User)
            class BadFilter:
                @filter_field
                def broken(self, value, query):
                    return query
