"""Tests for AbstractRepo enforcement in the Django backend."""

from types import SimpleNamespace

import pytest
import strawberry

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.repo import _check_auth
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
# Repo helpers
# ---------------------------------------------------------------------------


class DenyAllTagRepo(AbstractRepo[DjTag]):
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


class DenyUpdateTagRepo(AbstractRepo[DjTag]):
    def can_update(self, instance, data, info):
        return False


class DenyDeleteTagRepo(AbstractRepo[DjTag]):
    def can_delete(self, instance, info):
        return False


class DenyUnlinkTagRepo(AbstractRepo[DjTag]):
    def can_unlink(self, parent, field, instance, info):
        return False


class ScopingTagRepo(AbstractRepo[DjTag]):
    def scope_query(self, query, info):
        allowed = info.context.get("allowed_ids", [])
        return query.filter(pk__in=allowed)


class DenyAllUserRepo(AbstractRepo[DjUser]):
    def can_create(self, data, info):
        return False

    def can_update(self, instance, data, info):
        return False

    def can_delete(self, instance, info):
        return False


class DenyUpdateUserRepo(AbstractRepo[DjUser]):
    def can_update(self, instance, data, info):
        return False


class ScopingUserRepo(AbstractRepo[DjUser]):
    def scope_query(self, query, info):
        allowed = info.context.get("allowed_ids", [])
        return query.filter(pk__in=allowed)


class LifecycleTagRepo(AbstractRepo[DjTag]):
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
# Fixtures / seed
# ---------------------------------------------------------------------------


def _seed():
    alice = DjUser.objects.create(id=1, name="Alice", email="alice@example.com")
    bob = DjUser.objects.create(id=2, name="Bob", email="bob@example.com")

    python = DjTag.objects.create(id=1, name="python")
    graphql = DjTag.objects.create(id=2, name="graphql")

    p1 = DjPost.objects.create(
        id=1, title="Hello", body="World", is_published=True, author=alice
    )
    p1.tags.add(python, graphql)

    c1 = DjComment.objects.create(id=1, body="Nice!", post=p1, author=bob)

    return {
        "alice": alice,
        "bob": bob,
        "python": python,
        "graphql": graphql,
        "post": p1,
        "comment": c1,
    }


# ---------------------------------------------------------------------------
# _check_auth unit tests
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_none_repo_is_noop(self):
        _check_auth(None, "can_create", {}, None)

    def test_allow_repo_passes(self):
        class AllowRepo(AbstractRepo[DjUser]):
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
        assert DenyAllTagRepo.model is DjTag

    def test_model_explicit(self):
        class ExplicitRepo(AbstractRepo):
            model = DjUser

        assert ExplicitRepo.model is DjUser

    def test_model_none_without_param(self):
        class BareRepo(AbstractRepo):
            pass

        assert BareRepo.model is None


# ---------------------------------------------------------------------------
# apply_ref_list repo tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestRefListRepo:
    def test_create_denied_by_repo(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyAllTagRepo})
        info = SimpleNamespace(context={})

        @strawberry.input
        class DjRepoCreateTag:
            name: str

        ref_type = orm.ref(DjTag, create=DjRepoCreateTag)
        ref = ref_type(create=DjRepoCreateTag(name="denied"))

        with pytest.raises(PermissionError, match="can_create denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_update_denied_by_repo(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyUpdateTagRepo})
        info = SimpleNamespace(context={})

        @strawberry.input
        class DjRepoUpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(DjTag, update=DjRepoUpdateTag)
        ref = ref_type(update=DjRepoUpdateTag(id=strawberry.ID("1"), name="renamed"))

        with pytest.raises(PermissionError, match="can_update denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_delete_denied_by_repo(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyDeleteTagRepo})
        info = SimpleNamespace(context={})

        ref_type = orm.ref(DjTag, delete=True)

        @strawberry.input
        class DjRepoDeleteRef:
            id: strawberry.ID

        ref = ref_type(delete=DjRepoDeleteRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_delete denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_unlink_denied_by_repo(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyUnlinkTagRepo})
        info = SimpleNamespace(context={})

        ref_type = orm.ref(DjTag, unlink=True)

        @strawberry.input
        class DjRepoUnlinkRef:
            id: strawberry.ID

        ref = ref_type(unlink=DjRepoUnlinkRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_unlink denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_scope_query_hides_objects(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: ScopingTagRepo})
        info = SimpleNamespace(context={"allowed_ids": [999]})

        @strawberry.input
        class DjRepoUpdateTag2:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(DjTag, update=DjRepoUpdateTag2)
        ref = ref_type(update=DjRepoUpdateTag2(id=strawberry.ID("1"), name="renamed"))
        orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = DjTag.objects.get(pk=1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    def test_authorize_callback_takes_precedence(self, setup_tables):
        data = _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyAllTagRepo})
        info = SimpleNamespace(context={})

        @strawberry.input
        class DjRepoCreateTag2:
            name: str

        ref_type = orm.ref(DjTag, create=DjRepoCreateTag2)
        ref = ref_type(create=DjRepoCreateTag2(name="allowed"))

        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        assert DjTag.objects.filter(name="allowed").exists()

    def test_per_model_isolation(self, setup_tables):
        """A repo for DjTag should not affect DjUser operations."""
        _seed()
        orm = StrawberryORM.for_django(repos={DjTag: DenyAllTagRepo})

        repo = orm.backend.get_repo(DjUser)
        assert repo is None, "No repo registered for DjUser"

    def test_no_repos_allows_all(self, setup_tables):
        """Without repos, all operations should succeed."""
        data = _seed()
        orm = StrawberryORM.for_django()
        info = SimpleNamespace(context={})

        @strawberry.input
        class DjRepoCreateTag3:
            name: str

        ref_type = orm.ref(DjTag, create=DjRepoCreateTag3)
        ref = ref_type(create=DjRepoCreateTag3(name="unrestricted"))

        orm.apply_ref_list(data["post"], "tags", [ref], info)
        assert DjTag.objects.filter(name="unrestricted").exists()


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestLifecycleHooks:
    """Hooks fire on the writes the library performs, i.e. through ref lists."""

    def _ref_list(self, orm, post, ref):
        orm.apply_ref_list(post, "tags", [ref], SimpleNamespace(context={}))

    def test_on_before_create_transforms_data(self, setup_tables):
        data = _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_django()
        orm._backend._repos = {DjTag: LifecycleTagRepo}

        @strawberry.input
        class DjLifecycleCreateTag:
            name: str

        ref_type = orm.ref(DjTag, create=DjLifecycleCreateTag)
        self._ref_list(
            orm, data["post"], ref_type(create=DjLifecycleCreateTag(name="hooks"))
        )

        assert DjTag.objects.filter(name="HOOKS").exists(), (
            "on_before_create should have uppercased the name"
        )
        assert "before_create" in LifecycleTagRepo.calls
        assert "after_create" in LifecycleTagRepo.calls

    def test_on_before_update_called(self, setup_tables):
        data = _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_django()
        orm._backend._repos = {DjTag: LifecycleTagRepo}

        @strawberry.input
        class DjLifecycleUpdateTag:
            id: strawberry.ID
            name: str

        ref_type = orm.ref(DjTag, update=DjLifecycleUpdateTag)
        self._ref_list(
            orm,
            data["post"],
            ref_type(update=DjLifecycleUpdateTag(id="1", name="updated")),
        )

        assert "before_update" in LifecycleTagRepo.calls
        assert "after_update" in LifecycleTagRepo.calls
        data["python"].refresh_from_db()
        assert data["python"].name == "updated"

    def test_on_before_delete_called(self, setup_tables):
        data = _seed()
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_django()
        orm._backend._repos = {DjTag: LifecycleTagRepo}

        @strawberry.input
        class DjLifecycleDeleteTag:
            id: strawberry.ID

        ref_type = orm.ref(DjTag, delete=True)
        self._ref_list(orm, data["post"], ref_type(delete=DjLifecycleDeleteTag(id="1")))

        assert "before_delete" in LifecycleTagRepo.calls
        assert not DjTag.objects.filter(pk=1).exists()
