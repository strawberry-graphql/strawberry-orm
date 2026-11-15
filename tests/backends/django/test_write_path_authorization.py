from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.write_path_authorization import (
    AbstractTestAuthorizeKeepsTheRepo,
    AbstractTestAuthorizeSeesTheKey,
    AbstractTestLinkChecks,
    AbstractTestNodeInputsFollowTypeExclude,
    AbstractTestOnReplaceDelete,
    AbstractTestPermissionResults,
    AbstractTestPolicyWithRepos,
    AbstractTestProjectionDefaults,
    AbstractTestRefListDeleteUsesTheRepo,
    AbstractTestRefListIsAtomic,
    AbstractTestRefListMembership,
    AbstractTestUpsertSeed,
    WriteDb,
    resolve,
)
from tests.backends.django.models import Comment, Post, Tag, User

MODELS = SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


def _on_db_thread(call: Callable[[], Any]) -> Any:
    """Run *call* on the one thread every ``thread_sensitive`` call shares."""
    return sync_to_async(call, thread_sensitive=True)()


@dataclass
class DjangoWriteDb(WriteDb):
    #: Writes from a sync resolver on the database thread, or from async code.
    sync: bool = True

    async def write(self, call: Callable[[], Any]) -> Any:
        if self.sync:
            return await _on_db_thread(call)
        return await resolve(call())

    def new_orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            warn_sensitive=False,
            **kwargs,
        )

    def only(self, query: Any, model: type, pks: list[int]) -> Any:
        return query.filter(pk__in=pks)

    async def create(self, model: type, **values: Any) -> Any:
        return await _on_db_thread(lambda: model.objects.create(**values))

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        await _on_db_thread(lambda: getattr(instance, name).add(*rows))

    async def rows(self, model: type) -> list[Any]:
        return await _on_db_thread(lambda: list(model.objects.order_by("pk")))

    async def reload(self, instance: Any) -> Any:
        return await _on_db_thread(lambda: type(instance).objects.get(pk=instance.pk))

    async def related(self, instance: Any, name: str) -> list[Any]:
        return await _on_db_thread(lambda: list(getattr(instance, name).order_by("pk")))


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    return DjangoWriteDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestRefListMembership(AbstractTestRefListMembership):
    pass


class TestAuthorizeKeepsTheRepo(AbstractTestAuthorizeKeepsTheRepo):
    pass


class TestPermissionResults(AbstractTestPermissionResults):
    pass


class TestUpsertSeed(AbstractTestUpsertSeed):
    pass


class TestNodeInputsFollowTypeExclude(AbstractTestNodeInputsFollowTypeExclude):
    pass


class TestProjectionDefaults(AbstractTestProjectionDefaults):
    pass


class TestLinkChecks(AbstractTestLinkChecks):
    pass


class TestRefListIsAtomic(AbstractTestRefListIsAtomic):
    pass


class TestPolicyWithRepos(AbstractTestPolicyWithRepos):
    pass


class TestRefListDeleteUsesTheRepo(AbstractTestRefListDeleteUsesTheRepo):
    pass


class TestOnReplaceDelete(AbstractTestOnReplaceDelete):
    pass


class TestAuthorizeSeesTheKey(AbstractTestAuthorizeSeesTheKey):
    pass
