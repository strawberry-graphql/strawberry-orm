"""Direct permission checks refuse a hook they cannot wait for."""

import pytest

from strawberry_orm import AbstractRepo, MutationPolicy
from strawberry_orm.policy import _check_policy
from strawberry_orm.repo import _check_auth


class AsyncDenyRepo(AbstractRepo):
    async def can_delete(self, instance, info):
        return False


class AsyncDenyPolicy(MutationPolicy):
    async def can_delete(self, model, instance, info):
        return False


class TestDirectChecks:
    def test_an_async_repo_hook_is_refused_rather_than_allowed(self):
        repo = AsyncDenyRepo.__new__(AsyncDenyRepo)
        with pytest.raises(
            TypeError, match=r"AbstractRepo\.can_delete returned an awaitable"
        ):
            _check_auth(repo, "can_delete", object(), None)

    def test_an_async_policy_check_is_refused_rather_than_allowed(self):
        with pytest.raises(
            TypeError, match=r"MutationPolicy\.can_delete returned an awaitable"
        ):
            _check_policy(AsyncDenyPolicy(), "can_delete", object, object(), None)
