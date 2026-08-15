"""Tests for AbstractRepo enforcement in the SQLAlchemy backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import strawberry
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.repo import _check_auth
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
# Repo helpers
# ---------------------------------------------------------------------------


class DenyAllTagRepo(AbstractRepo[SATag]):
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


class DenyUpdateTagRepo(AbstractRepo[SATag]):
    def can_update(self, instance, data, info):
        return False


class DenyDeleteTagRepo(AbstractRepo[SATag]):
    def can_delete(self, instance, info):
        return False


class DenyUnlinkTagRepo(AbstractRepo[SATag]):
    def can_unlink(self, parent, field, instance, info):
        return False


class ScopingTagRepo(AbstractRepo[SATag]):
    def scope_query(self, query, info):
        from strawberry_orm.repo import _get_sa_pk_column

        pk_col = _get_sa_pk_column(self.model)
        allowed = info.context.get("allowed_ids", [])
        return query.where(pk_col.in_(allowed))


class DenyAllUserRepo(AbstractRepo[SAUser]):
    def can_create(self, data, info):
        return False

    def can_update(self, instance, data, info):
        return False

    def can_delete(self, instance, info):
        return False


class DenyUpdateUserRepo(AbstractRepo[SAUser]):
    def can_update(self, instance, data, info):
        return False


class ScopingUserRepo(AbstractRepo[SAUser]):
    def scope_query(self, query, info):
        from strawberry_orm.repo import _get_sa_pk_column

        pk_col = _get_sa_pk_column(self.model)
        allowed = info.context.get("allowed_ids", [])
        return query.where(pk_col.in_(allowed))


class LifecycleTagRepo(AbstractRepo[SATag]):
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
# _check_auth unit tests
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_none_repo_is_noop(self):
        _check_auth(None, "can_create", {}, None)

    def test_allow_repo_passes(self):
        class AllowRepo(AbstractRepo[SAUser]):
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
        assert DenyAllTagRepo.model is SATag

    def test_model_explicit(self):
        class ExplicitRepo(AbstractRepo):
            model = SAUser

        assert ExplicitRepo.model is SAUser

    def test_model_none_without_param(self):
        class BareRepo(AbstractRepo):
            pass

        assert BareRepo.model is None


# ---------------------------------------------------------------------------
# apply_ref_list repo tests
# ---------------------------------------------------------------------------


class TestRefListRepo:
    def test_create_denied_by_repo(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyAllTagRepo}
        )
        info = _make_info(session)

        @strawberry.input
        class SACreateTag:
            name: str

        ref_type = orm.ref(SATag, create=SACreateTag)
        ref = ref_type(create=SACreateTag(name="denied"))

        with pytest.raises(PermissionError, match="can_create denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_update_denied_by_repo(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyUpdateTagRepo}
        )
        info = _make_info(session)

        @strawberry.input
        class SAUpdateTag:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(SATag, update=SAUpdateTag)
        ref = ref_type(update=SAUpdateTag(id=strawberry.ID("1"), name="renamed"))

        with pytest.raises(PermissionError, match="can_update denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_delete_denied_by_repo(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyDeleteTagRepo}
        )
        info = _make_info(session)

        ref_type = orm.ref(SATag, delete=True)

        @strawberry.input
        class SADeleteRef:
            id: strawberry.ID

        ref = ref_type(delete=SADeleteRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_delete denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_unlink_denied_by_repo(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyUnlinkTagRepo}
        )
        info = _make_info(session)

        ref_type = orm.ref(SATag, unlink=True)

        @strawberry.input
        class SAUnlinkRef:
            id: strawberry.ID

        ref = ref_type(unlink=SAUnlinkRef(id=strawberry.ID("1")))

        with pytest.raises(PermissionError, match="can_unlink denied"):
            orm.apply_ref_list(data["post"], "tags", [ref], info)

    def test_scope_query_hides_objects(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: ScopingTagRepo}
        )
        info = _make_info(session, allowed_ids=[999])

        @strawberry.input
        class SAUpdateTag2:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        ref_type = orm.ref(SATag, update=SAUpdateTag2)
        ref = ref_type(update=SAUpdateTag2(id=strawberry.ID("1"), name="renamed"))
        orm.apply_ref_list(data["post"], "tags", [ref], info)

        tag = session.get(SATag, 1)
        assert tag.name == "python", "Scoped-out tag should not have been updated"

    def test_authorize_callback_takes_precedence(self):
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyAllTagRepo}
        )
        info = _make_info(session)

        @strawberry.input
        class SACreateTag2:
            name: str

        ref_type = orm.ref(SATag, create=SACreateTag2)
        ref = ref_type(create=SACreateTag2(name="allowed"))

        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref],
            info,
            authorize=lambda action, model, pk, info: True,
        )
        new_tags = [t.name for t in data["post"].tags]
        assert "allowed" in new_tags

    def test_per_model_isolation(self):
        """A repo for SATag should not affect SAUser operations."""
        session = _make_session()
        _seed(session)

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: DenyAllTagRepo}
        )

        repo = orm.backend.get_repo(SAUser)
        assert repo is None, "No repo registered for SAUser"

    def test_no_repos_allows_all(self):
        """Without repos, all operations should succeed."""
        session = _make_session()
        data = _seed(session)

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        info = _make_info(session)

        @strawberry.input
        class SACreateTag3:
            name: str

        ref_type = orm.ref(SATag, create=SACreateTag3)
        ref = ref_type(create=SACreateTag3(name="unrestricted"))

        orm.apply_ref_list(data["post"], "tags", [ref], info)
        new_tags = [t.name for t in data["post"].tags]
        assert "unrestricted" in new_tags


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    """Hooks fire on the writes the library performs, i.e. through ref lists."""

    def test_on_before_create_transforms_data(self):
        session = _make_session()
        data = _seed(session)
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        orm._backend._repos = {SATag: LifecycleTagRepo}

        @strawberry.input
        class SALifecycleCreateTag:
            name: str

        ref_type = orm.ref(SATag, create=SALifecycleCreateTag)
        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(create=SALifecycleCreateTag(name="hooks"))],
            _make_info(session),
        )
        session.commit()

        assert "HOOKS" in [tag.name for tag in data["post"].tags], (
            "on_before_create should have uppercased the name"
        )
        assert "before_create" in LifecycleTagRepo.calls
        assert "after_create" in LifecycleTagRepo.calls

    def test_on_before_update_called(self):
        session = _make_session()
        data = _seed(session)
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        orm._backend._repos = {SATag: LifecycleTagRepo}

        @strawberry.input
        class SALifecycleUpdateTag:
            id: strawberry.ID
            name: str

        ref_type = orm.ref(SATag, update=SALifecycleUpdateTag)
        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(update=SALifecycleUpdateTag(id="1", name="updated"))],
            _make_info(session),
        )
        session.commit()

        assert "before_update" in LifecycleTagRepo.calls
        assert "after_update" in LifecycleTagRepo.calls
        assert "updated" in [tag.name for tag in data["post"].tags]

    def test_on_before_delete_called(self):
        session = _make_session()
        data = _seed(session)
        LifecycleTagRepo.calls = []
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        orm._backend._repos = {SATag: LifecycleTagRepo}

        @strawberry.input
        class SALifecycleDeleteTag:
            id: strawberry.ID

        ref_type = orm.ref(SATag, delete=True)
        orm.apply_ref_list(
            data["post"],
            "tags",
            [ref_type(delete=SALifecycleDeleteTag(id="1"))],
            _make_info(session),
        )
        session.commit()

        assert "before_delete" in LifecycleTagRepo.calls
        assert session.get(SATag, 1) is None
