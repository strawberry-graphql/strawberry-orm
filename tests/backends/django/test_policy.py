"""Tests for MutationPolicy enforcement in the Django backend."""

from types import SimpleNamespace

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import MutationPolicy, StrawberryORM
from strawberry_orm.policy import _check_policy
from strawberry_orm.types import auto
from tests.backends.django.models import (
    Comment as DjComment,
)
from tests.backends.django.models import (
    Post as DjPost,
)
from tests.backends.django.models import (
    Tag as DjTag,
)
from tests.backends.django.models import (
    User as DjUser,
)

# ---------------------------------------------------------------------------
# Policy helpers
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
    def scope_query(self, model, query, info):
        allowed = info.context.get("allowed_ids", [])
        return query.filter(pk__in=allowed)


def _seed():
    alice = DjUser.objects.create(id=1, name="Alice", email="alice@example.com")
    bob = DjUser.objects.create(id=2, name="Bob", email="bob@example.com")

    python = DjTag.objects.create(id=1, name="python")
    graphql = DjTag.objects.create(id=2, name="graphql")

    p1 = DjPost.objects.create(
        id=1, title="Hello", body="World", is_published=True, author=alice
    )
    p1.tags.add(python, graphql)

    DjComment.objects.create(id=1, body="Nice!", post=p1, author=bob)

    return {
        "alice": alice,
        "bob": bob,
        "python": python,
        "graphql": graphql,
        "post": p1,
    }


# ---------------------------------------------------------------------------
# Unit tests for _check_policy helper
# ---------------------------------------------------------------------------


class TestCheckPolicy:
    def test_none_policy_is_noop(self):
        _check_policy(None, "can_create", DjUser, {}, None)

    def test_allow_policy_passes(self):
        _check_policy(MutationPolicy(), "can_create", DjUser, {}, None)

    def test_deny_policy_raises(self):
        with pytest.raises(PermissionError, match="can_create denied"):
            _check_policy(DenyAllPolicy(), "can_create", DjUser, {}, None)


# ---------------------------------------------------------------------------
# apply_ref_list policy tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestRefListPolicy:
    def test_create_denied_by_policy(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=DenyAllPolicy())

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(DjTag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="denied"))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_create denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_update_denied_by_policy(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=DenyUpdatePolicy())

        @strawberry.input
        class UpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(DjTag, update=UpdateTag)
        ref = ref_type(update=UpdateTag(id=strawberry.ID("1"), name="renamed"))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_update denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_delete_denied_by_policy(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=DenyDeletePolicy())

        @strawberry.input
        class DeleteRef:
            id: strawberry.ID

        ref_type = orm.ref(DjTag, delete=True)
        ref = ref_type(delete=DeleteRef(id=strawberry.ID("1")))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_delete denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_unlink_denied_by_policy(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=DenyUnlinkPolicy())

        @strawberry.input
        class UnlinkRef:
            id: strawberry.ID

        ref_type = orm.ref(DjTag, unlink=True)
        ref = ref_type(unlink=UnlinkRef(id=strawberry.ID("1")))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_unlink denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_scope_query_hides_objects(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=ScopingPolicy())

        @strawberry.input
        class UpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(DjTag, update=UpdateTag)
        ref = ref_type(update=UpdateTag(id=strawberry.ID("1"), name="renamed"))

        info = SimpleNamespace(context={"allowed_ids": [999]})

        orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = DjTag.objects.get(pk=1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    def test_authorize_callback_takes_precedence(self, setup_tables):
        data = _seed()
        orm = StrawberryORM("django", policy=DenyAllPolicy())

        @strawberry.input
        class CreateTag:
            name: str

        ref_type = orm.ref(DjTag, create=CreateTag)
        ref = ref_type(create=CreateTag(name="allowed"))

        info = SimpleNamespace(context={})

        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        assert DjTag.objects.filter(name="allowed").exists()


# ---------------------------------------------------------------------------
# Node mutation policy tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level node schema (types must be at module scope for resolution)
# ---------------------------------------------------------------------------

_policy_node_orm = StrawberryORM("django")


@_policy_node_orm.type(DjUser)
class _PolicyUserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_policy_node_orm.type(DjTag)
class _PolicyTagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_policy_node_orm.type(DjPost)
class _PolicyPostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto
    is_published: auto


@strawberry.type
class _PolicyNodeQuery:
    @strawberry.field
    def users(self) -> list[_PolicyUserNode]:
        return []

    @strawberry.field
    def tags(self) -> list[_PolicyTagNode]:
        return []

    @strawberry.field
    def posts(self) -> list[_PolicyPostNode]:
        return []


@strawberry.type
class _PolicyNodeMutation:
    create_node = _policy_node_orm.mutations.create_node(input_name="DjPolicyCIn")
    update_node = _policy_node_orm.mutations.update_node(input_name="DjPolicyUIn")


_policy_node_schema = strawberry.Schema(
    query=_PolicyNodeQuery, mutation=_PolicyNodeMutation
)


@pytest.mark.django_db(transaction=True)
class TestNodeMutationPolicy:
    def _set_policy(self, policy):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from strawberry_orm.policy import _policy_to_repos

            _policy_node_orm._backend._repos = _policy_to_repos(policy)

    def test_create_node_denied(self, setup_tables):
        _seed()
        self._set_policy(DenyAllPolicy())

        result = _policy_node_schema.execute_sync(
            """
            mutation {
                createNode(input: { user: { name: "Evil", email: "e@e.com" } }) {
                    __typename
                }
            }
            """,
        )
        assert result.errors is not None
        assert "can_create denied" in str(result.errors[0])

    def test_update_node_denied(self, setup_tables):
        _seed()
        self._set_policy(DenyUpdatePolicy())

        result = _policy_node_schema.execute_sync(
            """
            mutation {
                updateNode(input: { user: { id: "1", name: "Renamed" } }) {
                    __typename
                }
            }
            """,
        )
        assert result.errors is not None
        assert "can_update denied" in str(result.errors[0])

    def test_update_node_scoped(self, setup_tables):
        _seed()
        self._set_policy(ScopingPolicy())

        result = _policy_node_schema.execute_sync(
            """
            mutation {
                updateNode(input: { user: { id: "1", name: "Hacked" } }) {
                    __typename
                }
            }
            """,
            context_value={"allowed_ids": [999]},
        )
        assert result.errors is not None
        assert "does not exist" in str(result.errors[0])

        user = DjUser.objects.get(pk=1)
        assert user.name == "Alice"

    def test_default_policy_allows_all(self, setup_tables):
        _seed()
        self._set_policy(MutationPolicy())

        result = _policy_node_schema.execute_sync(
            """
            mutation {
                createNode(input: { user: { name: "New", email: "new@e.com" } }) {
                    __typename
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data["createNode"]["__typename"] == "Policyusernode"

    def teardown_method(self):
        _policy_node_orm._backend._repos = {}
