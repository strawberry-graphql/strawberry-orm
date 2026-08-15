"""Tests for MutationPolicy enforcement in the Tortoise backend."""

from types import SimpleNamespace

import pytest
import strawberry
from tortoise import Tortoise

from strawberry_orm import MutationPolicy, StrawberryORM
from strawberry_orm.policy import _check_policy
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
        orm = StrawberryORM.for_tortoise(policy=DenyAllPolicy())

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
        orm = StrawberryORM.for_tortoise(policy=DenyUpdatePolicy())

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
        orm = StrawberryORM.for_tortoise(policy=DenyDeletePolicy())

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
        orm = StrawberryORM.for_tortoise(policy=DenyUnlinkPolicy())

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
        orm = StrawberryORM.for_tortoise(policy=ScopingPolicy())

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
        orm = StrawberryORM.for_tortoise(policy=DenyAllPolicy())

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
