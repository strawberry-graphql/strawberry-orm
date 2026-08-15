"""Authorization / exposure regression tests for the SQLAlchemy backend."""

import strawberry
from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from strawberry_orm.types import auto
from tests.abstract.security_vulnerabilities import (
    AbstractTestSecurityVulnerabilities,
)
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class _SensitiveBase(DeclarativeBase):
    pass


class SensitiveEmployee(_SensitiveBase):
    __tablename__ = "sec_vuln_employee"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    salary: Mapped[int] = mapped_column(Integer)
    ssn: Mapped[int] = mapped_column(Integer)
    credit_card: Mapped[int] = mapped_column(Integer)


class TestSecurityVulnerabilities(AbstractTestSecurityVulnerabilities):
    @staticmethod
    def scope_to_published(qs):
        return qs.where(SAPost.is_published.is_(True))

    def sensitive_model(self):
        return SensitiveEmployee

    def build_traversal_schema(self, orm):
        import warnings

        user_filter = orm.filter(SAUser)
        post_filter = orm.filter(SAPost)

        @orm.type(SAUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.where(SAUser.name == "Alice")

        @orm.type(SAPost, filters=post_filter)
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
