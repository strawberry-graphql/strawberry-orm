"""Authorization / exposure regression tests for the Django backend."""

import pytest
import strawberry
from django.db import models

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.security_vulnerabilities import (
    AbstractTestSecurityVulnerabilities,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


@pytest.fixture
def run_materialized_parents():
    def _run(*, materialize):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(is_published=True)

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                queryset = DjUser.objects.order_by("id")
                return list(queryset) if materialize else queryset

        result = orm.schema(query=Query).execute_sync(
            "{ users { name posts { title } } }", context_value={}
        )
        assert result.errors is None, result.errors
        return result.data["users"]

    return _run


@pytest.fixture
def run_materialized_to_one():
    def _run(*, materialize):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.filter(name="Alice")

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                queryset = DjPost.objects.order_by("id")
                return list(queryset) if materialize else queryset

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title author { name } } }", context_value={}
        )
        assert result.errors is None, result.errors
        return result.data["posts"]

    return _run


class SensitiveEmployee(models.Model):
    """Sensitive-looking numeric columns; never queried, only introspected."""

    name = models.CharField(max_length=50)
    salary = models.IntegerField()
    ssn = models.IntegerField()
    credit_card = models.IntegerField()

    class Meta:
        app_label = "testapp"
        db_table = "sec_vuln_employee"


@pytest.mark.django_db
class TestSecurityVulnerabilities(AbstractTestSecurityVulnerabilities):
    @staticmethod
    def scope_to_published(qs):
        return qs.filter(is_published=True)

    def sensitive_model(self):
        return SensitiveEmployee

    def build_traversal_schema(self, orm):
        import warnings

        user_filter = orm.filter(DjUser)
        post_filter = orm.filter(DjPost)

        @orm.type(DjUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(DjPost, filters=post_filter)
        class PT:
            id: auto
            title: auto
            author: UT

        @strawberry.type
        class Query:
            posts: list[PT] = orm.field.auto()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            orm.schema(query=Query)
        return [str(warning.message) for warning in caught]
