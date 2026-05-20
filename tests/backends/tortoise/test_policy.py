"""Tests for MutationPolicy enforcement in the Tortoise backend."""

from types import SimpleNamespace

import pytest
import strawberry
from strawberry import relay
from tortoise import Tortoise

from strawberry_orm import MutationPolicy, StrawberryORM
from strawberry_orm.policy import _check_policy
from strawberry_orm.types import auto
from tests.backends.tortoise.models import (
    Comment as TortComment,
)
from tests.backends.tortoise.models import (
    Post as TortPost,
)
from tests.backends.tortoise.models import (
    Tag as TortTag,
)
from tests.backends.tortoise.models import (
    User as TortUser,
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


async def _seed():
    alice = await TortUser.create(id=1, name="Alice", email="alice@example.com")
    bob = await TortUser.create(id=2, name="Bob", email="bob@example.com")

    python = await TortTag.create(id=1, name="python")
    graphql = await TortTag.create(id=2, name="graphql")

    p1 = await TortPost.create(
        id=1, title="Hello", body="World", is_published=True, author=alice
    )
    await p1.tags.add(python, graphql)

    await TortComment.create(id=1, body="Nice!", post=p1, author=bob)

    return {
        "alice": alice,
        "bob": bob,
        "python": python,
        "graphql": graphql,
        "post": p1,
    }


@pytest.fixture
async def tortoise_db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["tests.backends.tortoise.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


# ---------------------------------------------------------------------------
# Unit tests for _check_policy helper
# ---------------------------------------------------------------------------


class TestCheckPolicy:
    def test_none_policy_is_noop(self):
        _check_policy(None, "can_create", TortUser, {}, None)

    def test_allow_policy_passes(self):
        _check_policy(MutationPolicy(), "can_create", TortUser, {}, None)

    def test_deny_policy_raises(self):
        with pytest.raises(PermissionError, match="can_create denied"):
            _check_policy(DenyAllPolicy(), "can_create", TortUser, {}, None)


# ---------------------------------------------------------------------------
# apply_ref_list policy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRefListPolicy:
    async def test_create_denied_by_policy(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=DenyAllPolicy())

        @strawberry.input
        class TortCreateTag:
            name: str

        ref_type = orm.ref(TortTag, create=TortCreateTag)
        ref = ref_type(create=TortCreateTag(name="denied"))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_create denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_update_denied_by_policy(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=DenyUpdatePolicy())

        @strawberry.input
        class TortUpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(TortTag, update=TortUpdateTag)
        ref = ref_type(update=TortUpdateTag(id=strawberry.ID("1"), name="renamed"))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_update denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_delete_denied_by_policy(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=DenyDeletePolicy())

        @strawberry.input
        class TortDeleteRef:
            id: strawberry.ID

        ref_type = orm.ref(TortTag, delete=True)
        ref = ref_type(delete=TortDeleteRef(id=strawberry.ID("1")))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_delete denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_unlink_denied_by_policy(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=DenyUnlinkPolicy())

        @strawberry.input
        class TortUnlinkRef:
            id: strawberry.ID

        ref_type = orm.ref(TortTag, unlink=True)
        ref = ref_type(unlink=TortUnlinkRef(id=strawberry.ID("1")))

        info = SimpleNamespace(context={})

        with pytest.raises(PermissionError, match="can_unlink denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_scope_query_hides_objects(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=ScopingPolicy())

        @strawberry.input
        class TortUpdateTag2:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(TortTag, update=TortUpdateTag2)
        ref = ref_type(update=TortUpdateTag2(id=strawberry.ID("1"), name="renamed"))

        info = SimpleNamespace(context={"allowed_ids": [999]})

        await orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = await TortTag.get(pk=1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    async def test_authorize_callback_takes_precedence(self, tortoise_db):
        data = await _seed()
        orm = StrawberryORM.for_tortoise( policy=DenyAllPolicy())

        @strawberry.input
        class TortCreateTag2:
            name: str

        ref_type = orm.ref(TortTag, create=TortCreateTag2)
        ref = ref_type(create=TortCreateTag2(name="allowed"))

        info = SimpleNamespace(context={})

        await orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        assert await TortTag.filter(name="allowed").exists()


# ---------------------------------------------------------------------------
# Node mutation policy tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level node schema (types must be at module scope for resolution)
# ---------------------------------------------------------------------------

_policy_node_orm = StrawberryORM.for_tortoise()


@_policy_node_orm.type(TortUser)
class _PolicyUserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_policy_node_orm.type(TortTag)
class _PolicyTagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_policy_node_orm.type(TortPost)
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
    create_node = _policy_node_orm.mutations.create_node(input_name="TortPolicyCIn")
    update_node = _policy_node_orm.mutations.update_node(input_name="TortPolicyUIn")


_policy_node_schema = strawberry.Schema(
    query=_PolicyNodeQuery, mutation=_PolicyNodeMutation
)


@pytest.mark.asyncio
class TestNodeMutationPolicy:
    def _set_policy(self, policy):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from strawberry_orm.policy import _policy_to_repos

            _policy_node_orm._backend._repos = _policy_to_repos(policy)

    async def test_create_node_denied(self, tortoise_db):
        await _seed()
        self._set_policy(DenyAllPolicy())

        result = await _policy_node_schema.execute(
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

    async def test_update_node_denied(self, tortoise_db):
        await _seed()
        self._set_policy(DenyUpdatePolicy())

        result = await _policy_node_schema.execute(
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

    async def test_update_node_scoped(self, tortoise_db):
        await _seed()
        self._set_policy(ScopingPolicy())

        result = await _policy_node_schema.execute(
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

        user = await TortUser.get(pk=1)
        assert user.name == "Alice"

    async def test_default_policy_allows_all(self, tortoise_db):
        await _seed()
        self._set_policy(MutationPolicy())

        result = await _policy_node_schema.execute(
            """
            mutation {
                createNode(input: { user: { name: "New", email: "new@e.com" } }) {
                    __typename
                }
            }
            """,
        )
        assert result.errors is None
        assert "Policyusernode" in result.data["createNode"]["__typename"]

    def teardown_method(self):
        _policy_node_orm._backend._repos = {}
