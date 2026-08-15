"""Tests for AbstractRepo enforcement in the Tortoise backend."""

from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.repo import _check_auth
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
# Repo helpers
# ---------------------------------------------------------------------------


class DenyAllTagRepo(AbstractRepo[TortTag]):
    def can_create(self, data, info):
        return False

    def can_update(self, instance, data, info):
        return False

    def can_delete(self, instance, info):
        return False

    def can_link(self, parent, field, instance, info):
        return False

    def can_unlink(self, parent, field, instance, info):
        return False


class DenyUpdateTagRepo(AbstractRepo[TortTag]):
    def can_update(self, instance, data, info):
        return False


class DenyDeleteTagRepo(AbstractRepo[TortTag]):
    def can_delete(self, instance, info):
        return False


class DenyUnlinkTagRepo(AbstractRepo[TortTag]):
    def can_unlink(self, parent, field, instance, info):
        return False


class ScopingTagRepo(AbstractRepo[TortTag]):
    def scope_query(self, query, info):
        allowed = info.context.get("allowed_ids", [])
        return query.filter(pk__in=allowed)


class DenyAllUserRepo(AbstractRepo[TortUser]):
    def can_create(self, data, info):
        return False

    def can_update(self, instance, data, info):
        return False

    def can_delete(self, instance, info):
        return False


class DenyUpdateUserRepo(AbstractRepo[TortUser]):
    def can_update(self, instance, data, info):
        return False


class ScopingUserRepo(AbstractRepo[TortUser]):
    def scope_query(self, query, info):
        allowed = info.context.get("allowed_ids", [])
        return query.filter(pk__in=allowed)


class LifecycleTagRepo(AbstractRepo[TortTag]):
    """Records lifecycle hook invocations for testing."""

    calls: list[str] = []

    def on_before_create(self, data, info):
        LifecycleTagRepo.calls.append("before_create")
        data["name"] = data["name"].upper()
        return data

    def on_after_create(self, instance, info):
        LifecycleTagRepo.calls.append("after_create")

    def on_before_update(self, instance, data, info):
        LifecycleTagRepo.calls.append("before_update")
        return data

    def on_after_update(self, instance, info):
        LifecycleTagRepo.calls.append("after_update")

    def on_before_delete(self, instance, info):
        LifecycleTagRepo.calls.append("before_delete")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed():
    alice = await TortUser.create(id=1, name="Alice", email="alice@example.com")
    bob = await TortUser.create(id=2, name="Bob", email="bob@example.com")

    python = await TortTag.create(id=1, name="python")
    graphql = await TortTag.create(id=2, name="graphql")

    p1 = await TortPost.create(
        id=1, title="Hello", body="World", is_published=True, author=alice
    )
    await p1.tags.add(python, graphql)

    c1 = await TortComment.create(id=1, body="Nice!", post=p1, author=bob)

    return {
        "alice": alice,
        "bob": bob,
        "python": python,
        "graphql": graphql,
        "post": p1,
        "comment": c1,
    }


def _make_info(**extra):
    return SimpleNamespace(context={**extra})


# ---------------------------------------------------------------------------
# _check_auth unit tests
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_none_repo_is_noop(self):
        _check_auth(None, "can_create", {}, None)

    def test_allow_repo_passes(self):
        class AllowRepo(AbstractRepo[TortUser]):
            pass

        repo = AllowRepo.__new__(AllowRepo)
        _check_auth(repo, "can_create", {}, None)

    def test_deny_repo_raises(self):
        repo = DenyAllTagRepo.__new__(DenyAllTagRepo)
        with pytest.raises(PermissionError, match="can_create denied"):
            _check_auth(repo, "can_create", {}, None)


# ---------------------------------------------------------------------------
# AbstractRepo model extraction
# ---------------------------------------------------------------------------


class TestRepoModelExtraction:
    def test_model_from_generic(self):
        assert DenyAllTagRepo.model is TortTag

    def test_model_explicit(self):
        class ExplicitRepo(AbstractRepo):
            model = TortUser

        assert ExplicitRepo.model is TortUser

    def test_model_none_without_param(self):
        class BareRepo(AbstractRepo):
            pass

        assert BareRepo.model is None


# ---------------------------------------------------------------------------
# apply_ref_list repo tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRefListRepo:
    async def test_create_denied_by_repo(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyAllTagRepo})
        info = _make_info()

        @strawberry.input
        class TortCreateTag:
            name: str

        ref_type = orm.ref(TortTag, create=TortCreateTag)
        ref = ref_type(create=TortCreateTag(name="denied"))

        with pytest.raises(PermissionError, match="can_create denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_update_denied_by_repo(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyUpdateTagRepo})
        info = _make_info()

        @strawberry.input
        class TortUpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(TortTag, update=TortUpdateTag)
        ref = ref_type(update=TortUpdateTag(id=strawberry.ID("1"), name="renamed"))

        with pytest.raises(PermissionError, match="can_update denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_delete_denied_by_repo(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyDeleteTagRepo})
        info = _make_info()

        ref_type = orm.ref(TortTag, delete=True)

        @strawberry.input
        class TortDeleteRef:
            id: strawberry.ID

        ref = ref_type(delete=TortDeleteRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_delete denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_unlink_denied_by_repo(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyUnlinkTagRepo})
        info = _make_info()

        ref_type = orm.ref(TortTag, unlink=True)

        @strawberry.input
        class TortUnlinkRef:
            id: strawberry.ID

        ref = ref_type(unlink=TortUnlinkRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_unlink denied"):
            await orm.apply_ref_list(data["post"], "tags", [ref], info)

    async def test_scope_query_hides_objects(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: ScopingTagRepo})
        info = _make_info(allowed_ids=[999])

        @strawberry.input
        class TortUpdateTag2:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(TortTag, update=TortUpdateTag2)
        ref = ref_type(update=TortUpdateTag2(id=strawberry.ID("1"), name="renamed"))
        await orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = await TortTag.get(pk=1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    async def test_authorize_callback_takes_precedence(self, tortoise_db):
        data = await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyAllTagRepo})
        info = _make_info()

        @strawberry.input
        class TortCreateTag2:
            name: str

        ref_type = orm.ref(TortTag, create=TortCreateTag2)
        ref = ref_type(create=TortCreateTag2(name="allowed"))

        await orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        tags = await data["post"].tags.all()
        new_tags = [t.name for t in tags]
        assert "allowed" in new_tags

    async def test_per_model_isolation(self, tortoise_db):
        """A repo for TortTag should not affect TortUser operations."""
        await _seed()

        orm = StrawberryORM.for_tortoise(repos={TortTag: DenyAllTagRepo})

        repo = orm.backend.get_repo(TortUser)
        assert repo is None, "No repo registered for TortUser"

    async def test_no_repos_allows_all(self, tortoise_db):
        """Without repos, all operations should succeed."""
        data = await _seed()

        orm = StrawberryORM.for_tortoise()
        info = _make_info()

        @strawberry.input
        class TortCreateTag3:
            name: str

        ref_type = orm.ref(TortTag, create=TortCreateTag3)
        ref = ref_type(create=TortCreateTag3(name="unrestricted"))

        await orm.apply_ref_list(data["post"], "tags", [ref], info)
        tags = await data["post"].tags.all()
        new_tags = [t.name for t in tags]
        assert "unrestricted" in new_tags


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    """Hooks fire on the writes the library performs, i.e. through ref lists."""

    async def test_on_before_create_transforms_data(self, tortoise_db):
        data = await _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_tortoise()
        orm._backend._repos = {TortTag: LifecycleTagRepo}

        @strawberry.input
        class TortLifecycleCreateTag:
            name: str

        ref_type = orm.ref(TortTag, create=TortLifecycleCreateTag)
        await orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(create=TortLifecycleCreateTag(name="hooks"))],
            _make_info(),
        )

        assert await TortTag.filter(name="HOOKS").exists(), (
            "on_before_create should have uppercased the name"
        )
        assert "before_create" in LifecycleTagRepo.calls
        assert "after_create" in LifecycleTagRepo.calls

    async def test_on_before_update_called(self, tortoise_db):
        data = await _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_tortoise()
        orm._backend._repos = {TortTag: LifecycleTagRepo}

        @strawberry.input
        class TortLifecycleUpdateTag:
            id: strawberry.ID
            name: str

        ref_type = orm.ref(TortTag, update=TortLifecycleUpdateTag)
        await orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(update=TortLifecycleUpdateTag(id="1", name="updated"))],
            _make_info(),
        )

        assert "before_update" in LifecycleTagRepo.calls
        assert "after_update" in LifecycleTagRepo.calls
        assert await TortTag.filter(name="updated").exists()

    async def test_on_before_delete_called(self, tortoise_db):
        data = await _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_tortoise()
        orm._backend._repos = {TortTag: LifecycleTagRepo}

        @strawberry.input
        class TortLifecycleDeleteTag:
            id: strawberry.ID

        ref_type = orm.ref(TortTag, delete=True)
        await orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(delete=TortLifecycleDeleteTag(id="1"))],
            _make_info(),
        )

        assert "before_delete" in LifecycleTagRepo.calls
        assert not await TortTag.filter(pk=1).exists()
