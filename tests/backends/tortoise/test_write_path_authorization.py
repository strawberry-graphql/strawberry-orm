from types import SimpleNamespace
from typing import Any

import pytest_asyncio

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
)
from tests.backends.tortoise.models import Comment, Post, Tag, User

MODELS = SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TortoiseWriteDb(WriteDb):
    def new_orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
            warn_sensitive=False,
            **kwargs,
        )

    def only(self, query: Any, model: type, pks: list[int]) -> Any:
        return query.filter(pk__in=pks)

    async def create(self, model: type, **values: Any) -> Any:
        return await model.create(**values)

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        await getattr(instance, name).add(*rows)

    async def rows(self, model: type) -> list[Any]:
        return await model.all().order_by(model._meta.pk_attr)

    async def reload(self, instance: Any) -> Any:
        return await type(instance).get(pk=instance.pk)

    async def related(self, instance: Any, name: str) -> list[Any]:
        return await getattr(instance, name).all().order_by("id")


@pytest_asyncio.fixture
async def writes(tortoise_db):
    return TortoiseWriteDb(MODELS, async_hooks=True)


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
