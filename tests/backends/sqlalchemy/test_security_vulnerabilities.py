"""Authorization / exposure regression tests for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.security_vulnerabilities import (
    AbstractTestSecurityVulnerabilities,
)
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _orm(sa_session):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        session_getter=lambda info: info.context["session"],
        warn_missing_scope=False,
    )


@pytest.fixture
def run_materialized_parents(sa_session):
    def _run(*, materialize):
        # Without this the identity map can hand back a collection loaded
        # (and scoped) by an earlier run, hiding the very leak under test.
        sa_session.expunge_all()
        orm = _orm(sa_session)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(SAPost.is_published.is_(True))

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                stmt = select(SAUser).order_by(SAUser.id)
                if materialize:
                    return list(sa_session.execute(stmt).unique().scalars().all())
                return stmt

        result = orm.schema(query=Query).execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        return result.data["users"]

    return _run


@pytest.fixture
def run_materialized_to_one(sa_session):
    def _run(*, materialize):
        sa_session.expunge_all()
        orm = _orm(sa_session)

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(SAUser.name == "Alice")

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            author: UserType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(SAPost).order_by(SAPost.id)
                if materialize:
                    return list(sa_session.execute(stmt).unique().scalars().all())
                return stmt

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        return result.data["posts"]

    return _run


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
