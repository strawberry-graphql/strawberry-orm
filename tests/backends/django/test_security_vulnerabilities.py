"""Authorization / exposure regression tests for the Django backend."""

import strawberry
from django.db import models

from strawberry_orm.types import auto
from tests.abstract.security_vulnerabilities import (
    AbstractTestSecurityVulnerabilities,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class SensitiveEmployee(models.Model):
    """Sensitive-looking numeric columns; never queried, only introspected."""

    name = models.CharField(max_length=50)
    salary = models.IntegerField()
    ssn = models.IntegerField()
    credit_card = models.IntegerField()

    class Meta:
        app_label = "testapp"
        db_table = "sec_vuln_employee"


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
