"""Tests for MutationPolicy enforcement in the SQLAlchemy backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from strawberry import relay

from strawberry_orm import MutationPolicy, StrawberryORM
from strawberry_orm.policy import _check_policy
from strawberry_orm.types import auto
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
        from strawberry_orm.backends.sqlalchemy import _get_sa_pk_column

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
# Unit tests for _check_policy helper
# ---------------------------------------------------------------------------


class TestCheckPolicy:
    def test_none_policy_is_noop(self):
        _check_policy(None, "can_create", SAUser, {}, None)

    def test_allow_policy_passes(self):
        _check_policy(MutationPolicy(), "can_create", SAUser, {}, None)

    def test_deny_policy_raises(self):
        with pytest.raises(PermissionError, match="can_create denied"):
            _check_policy(DenyAllPolicy(), "can_create", SAUser, {}, None)


# ---------------------------------------------------------------------------
# apply_ref_list policy tests
# ---------------------------------------------------------------------------


class TestRefListPolicy:
    def test_create_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=DenyAllPolicy())
        info = _make_info(session)

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(SATag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="denied"))

        with pytest.raises(PermissionError, match="can_create denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_update_denied_by_policy(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=DenyUpdatePolicy())
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

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=DenyDeletePolicy())
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

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=DenyUnlinkPolicy())
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

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=ScopingPolicy())
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

    def test_authorize_callback_takes_precedence(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=DenyAllPolicy())
        info = _make_info(session)

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(SATag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="allowed"))

        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        new_tags = [t.name for t in data["post"].tags]
        assert "allowed" in new_tags


# ---------------------------------------------------------------------------
# Node mutation policy tests
# ---------------------------------------------------------------------------


class TestNodeMutationPolicy:
    def _build_schema(self, policy):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite", policy=policy)

        @orm.type(SAUser)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @orm.type(SATag)
        class TagNode(relay.Node):
            id: relay.NodeID[int]
            name: auto

        @orm.type(SAPost)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto
            body: auto
            is_published: auto

        @strawberry.type
        class Q:
            @strawberry.field
            def ok(self) -> bool:
                return True

        @strawberry.type
        class M:
            create_node = orm.mutations.create_node(input_name="CIn")
            update_node = orm.mutations.update_node(input_name="UIn")

        return strawberry.Schema(
            query=Q, mutation=M, types=[UserNode, TagNode, PostNode]
        )

    def test_create_node_denied(self):
        session = _make_session()
        _seed(session)
        schema = self._build_schema(DenyAllPolicy())

        result = schema.execute_sync(
            """
            mutation {
                createNode(input: { user: { name: "Evil", email: "e@e.com" } }) {
                    __typename
                }
            }
            """,
            context_value={"session": session},
        )
        assert result.errors is not None
        assert "can_create denied" in str(result.errors[0])

    def test_update_node_denied(self):
        session = _make_session()
        _seed(session)
        schema = self._build_schema(DenyUpdatePolicy())

        result = schema.execute_sync(
            """
            mutation {
                updateNode(input: { user: { id: "1", name: "Renamed" } }) {
                    __typename
                }
            }
            """,
            context_value={"session": session},
        )
        assert result.errors is not None
        assert "can_update denied" in str(result.errors[0])

    def test_update_node_scoped(self):
        """scope_query should prevent loading the instance entirely."""
        session = _make_session()
        _seed(session)
        schema = self._build_schema(ScopingPolicy())

        result = schema.execute_sync(
            """
            mutation {
                updateNode(input: { user: { id: "1", name: "Hacked" } }) {
                    __typename
                }
            }
            """,
            context_value={"session": session, "allowed_ids": [999]},
        )
        assert result.errors is not None
        assert "does not exist" in str(result.errors[0])

        user = session.get(SAUser, 1)
        assert user.name == "Alice"

    def test_default_policy_allows_all(self):
        session = _make_session()
        _seed(session)
        schema = self._build_schema(MutationPolicy())

        result = schema.execute_sync(
            """
            mutation {
                createNode(input: { user: { name: "New", email: "new@e.com" } }) {
                    __typename
                }
            }
            """,
            context_value={"session": session},
        )
        assert result.errors is None
        assert result.data["createNode"]["__typename"] == "UserNode"
