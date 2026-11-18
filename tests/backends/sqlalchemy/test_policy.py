"""Tests for MutationPolicy enforcement in the SQLAlchemy backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from strawberry_orm import MutationPolicy, StrawberryORM
from tests.backends.sqlalchemy.models import (
    Base as SABase,
)
from tests.backends.sqlalchemy.models import (
    Comment as SAComment,
)
from tests.backends.sqlalchemy.models import (
    Post as SAPost,
)
from tests.backends.sqlalchemy.models import (
    Tag as SATag,
)
from tests.backends.sqlalchemy.models import (
    User as SAUser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DenyAllPolicy(MutationPolicy):
    def can_create(self, model, data, info):
        return False

    def can_update(self, model, instance, data, info):
        return False

    def can_delete(self, model, instance, info):
        return False

    def can_link(self, parent, field, instance, info):
        return False

    def can_unlink(self, parent, field, instance, info):
        return False


class DenyUpdatePolicy(MutationPolicy):
    def can_update(self, model, instance, data, info):
        return False


class DenyDeletePolicy(MutationPolicy):
    def can_delete(self, model, instance, info):
        return False


class DenyUnlinkPolicy(MutationPolicy):
    def can_unlink(self, parent, field, instance, info):
        return False


class ScopingPolicy(MutationPolicy):
    """Only allows access to objects where the PK matches the context's allowed_ids."""

    def scope_query(self, model, query, info):
        from strawberry_orm.repo import _get_sa_pk_column

        pk_col = _get_sa_pk_column(model)
        allowed = info.context.get("allowed_ids", [])
        return query.where(pk_col.in_(allowed))


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    SABase.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_info(session, **extra):
    ctx = {"session": session, **extra}
    return SimpleNamespace(context=ctx)


def _seed(session):
    alice = SAUser(id=1, name="Alice", email="alice@example.com")
    bob = SAUser(id=2, name="Bob", email="bob@example.com")
    session.add_all([alice, bob])
    session.flush()

    python = SATag(id=1, name="python")
    graphql = SATag(id=2, name="graphql")
    session.add_all([python, graphql])
    session.flush()

    p1 = SAPost(id=1, title="Hello", body="World", is_published=True, author_id=1)
    session.add(p1)
    session.flush()
    p1.tags.extend([python, graphql])
    session.flush()

    c1 = SAComment(id=1, body="Nice!", post_id=1, author_id=2)
    session.add(c1)
    session.commit()

    return {
        "alice": alice,
        "bob": bob,
        "python": python,
        "graphql": graphql,
        "post": p1,
        "comment": c1,
    }


# ---------------------------------------------------------------------------
# apply_ref_list policy tests
# ---------------------------------------------------------------------------


class TestRefListPolicy:
    def test_create_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyAllPolicy())
        info = _make_info(session)

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(SATag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="denied"))

        with pytest.raises(PermissionError, match="can_create denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_create_allowed_by_a_policy_that_keeps_the_default(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyUpdatePolicy())
        info = _make_info(session)

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(SATag, create=CreateTag)
        orm.apply_ref_list(
            data["post"], "tags", [ref_type(create=CreateTag(name="go"))], info
        )
        session.commit()
        assert sorted(tag.name for tag in data["post"].tags) == [
            "go",
            "graphql",
            "python",
        ]

    def test_update_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyUpdatePolicy())
        info = _make_info(session)

        @strawberry.input
        class UpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(SATag, update=UpdateTag)
        ref = ref_type(update=UpdateTag(id=strawberry.ID("1"), name="renamed"))

        with pytest.raises(PermissionError, match="can_update denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_delete_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyDeletePolicy())
        info = _make_info(session)

        @strawberry.input
        class DeleteRef:
            id: strawberry.ID

        ref_type = orm.ref(SATag, delete=True)
        ref = ref_type(delete=DeleteRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_delete denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_unlink_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyUnlinkPolicy())
        info = _make_info(session)

        @strawberry.input
        class UnlinkRef:
            id: strawberry.ID

        ref_type = orm.ref(SATag, unlink=True)
        ref = ref_type(unlink=UnlinkRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_unlink denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_scope_query_hides_objects(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=ScopingPolicy())
        info = _make_info(session, allowed_ids=[999])

        @strawberry.input
        class UpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(SATag, update=UpdateTag)
        ref = ref_type(update=UpdateTag(id=strawberry.ID("1"), name="renamed"))
        orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = session.get(SATag, 1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    def test_authorize_does_not_override_the_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", policy=DenyAllPolicy())
        info = _make_info(session)

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(SATag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="allowed"))

        with pytest.raises(PermissionError, match="can_create"):
            orm.apply_ref_list(
                data["post"],
                "tags",
                [ref],
                info,
                authorize=lambda action, model, pk, info: True,
            )
        new_tags = [t.name for t in data["post"].tags]
        assert "allowed" not in new_tags
