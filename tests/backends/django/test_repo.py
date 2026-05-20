"""Tests for AbstractRepo enforcement in the Django backend."""

from types import SimpleNamespace

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.repo import _check_auth
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
# Module-level node schema (single ORM instance — registering the same Django
# models on multiple StrawberryORM.for_django() instances breaks `auto` / Node resolution)
# ---------------------------------------------------------------------------


_repo_node_orm = StrawberryORM.for_django()


@_repo_node_orm.type(DjUser)
class _RepoUserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto


@_repo_node_orm.type(DjTag)
class _RepoTagNode(relay.Node):
    id: relay.NodeID[int]
    name: auto


@_repo_node_orm.type(DjPost)
class _RepoPostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto
    is_published: auto


@strawberry.type
class _RepoNodeQuery:
    @strawberry.field
    def users(self) -> list[_RepoUserNode]:
        return []

    @strawberry.field
    def tags(self) -> list[_RepoTagNode]:
        return []

    @strawberry.field
    def posts(self) -> list[_RepoPostNode]:
        return []


@strawberry.type
class _RepoNodeMutation:
    create_node = _repo_node_orm.mutations.create_node(input_name="DjRepoCIn")
    update_node = _repo_node_orm.mutations.update_node(input_name="DjRepoUIn")


_repo_node_schema = strawberry.Schema(
    query=_RepoNodeQuery,
    mutation=_RepoNodeMutation,
)


@pytest.mark.django_db(transaction=True)
class TestLifecycleHooks:
    def test_on_before_create_transforms_data(self, setup_tables):
        _seed()
        LifecycleTagRepo.calls = []
        _repo_node_orm._backend._repos = {DjTag: LifecycleTagRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    createNode(input: { tag: { name: "hooks" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is None
        tag = DjTag.objects.filter(name="HOOKS").first()
        assert tag is not None, "on_before_create should have uppercased the name"
        assert "before_create" in LifecycleTagRepo.calls
        assert "after_create" in LifecycleTagRepo.calls

    def test_on_before_update_called(self, setup_tables):
        _seed()
        LifecycleTagRepo.calls = []
        _repo_node_orm._backend._repos = {DjTag: LifecycleTagRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    updateNode(input: { tag: { id: "1", name: "updated" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is None
        assert "before_update" in LifecycleTagRepo.calls
        assert "after_update" in LifecycleTagRepo.calls

    def teardown_method(self):
        _repo_node_orm._backend._repos = {}


@pytest.mark.django_db(transaction=True)
class TestNodeMutationRepo:
    def test_create_node_denied(self, setup_tables):
        _seed()
        _repo_node_orm._backend._repos = {DjUser: DenyAllUserRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    createNode(input: { user: { name: "Evil", email: "e@e.com" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is not None
        assert "can_create denied" in str(result.errors[0])

    def test_update_node_denied(self, setup_tables):
        _seed()
        _repo_node_orm._backend._repos = {DjUser: DenyUpdateUserRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    updateNode(input: { user: { id: "1", name: "Renamed" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is not None
        assert "can_update denied" in str(result.errors[0])

    def test_update_node_scoped(self, setup_tables):
        _seed()
        _repo_node_orm._backend._repos = {DjUser: ScopingUserRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    updateNode(input: { user: { id: "1", name: "Hacked" } }) {
                        __typename
                    }
                }
                """,
                context_value={"allowed_ids": [999]},
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is not None
        assert "does not exist" in str(result.errors[0])

        user = DjUser.objects.get(pk=1)
        assert user.name == "Alice"

    def test_default_repo_allows_all(self, setup_tables):
        _seed()

        class AllowAllUserRepo(AbstractRepo[DjUser]):
            pass

        _repo_node_orm._backend._repos = {DjUser: AllowAllUserRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    createNode(input: { user: { name: "New", email: "new@e.com" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is None
        assert result.data["createNode"]["__typename"] == "Repousernode"

    def test_repo_only_affects_registered_model(self, setup_tables):
        """A repo for DjUser should not block Tag creation."""
        _seed()
        _repo_node_orm._backend._repos = {DjUser: DenyAllUserRepo}
        try:
            result = _repo_node_schema.execute_sync(
                """
                mutation {
                    createNode(input: { tag: { name: "NewTag" } }) {
                        __typename
                    }
                }
                """,
            )
        finally:
            _repo_node_orm._backend._repos = {}

        assert result.errors is None
        assert result.data["createNode"]["__typename"] == "Repotagnode"

    def teardown_method(self):
        _repo_node_orm._backend._repos = {}
