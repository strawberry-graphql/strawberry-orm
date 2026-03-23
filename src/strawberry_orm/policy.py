"""Mutation authorization policy for strawberry-orm.

.. deprecated::
    ``MutationPolicy`` is superseded by :class:`~strawberry_orm.repo.AbstractRepo`.
    Passing ``policy=`` to :class:`StrawberryORM` still works and is automatically
    converted into a global repo, but new code should use ``repos=`` instead.
"""

from __future__ import annotations

import warnings
from typing import Any


class MutationPolicy:
    """Base class for mutation authorization policies.

    .. deprecated::
        Use :class:`~strawberry_orm.repo.AbstractRepo` instead.

    Subclass and override methods to enforce per-operation authorization.
    Every method receives the Strawberry ``info`` object so you can access
    the current user, request, tenant, etc.

    Return ``True`` to allow an operation or ``False`` to deny it (which
    raises ``PermissionError``).

    Usage::

        class MyPolicy(MutationPolicy):
            def can_update(self, model, instance, data, info):
                return instance.author_id == info.context["user"].id

            def scope_query(self, model, query, info):
                return query.filter(tenant_id=info.context["tenant_id"])

        orm = StrawberryORM("sqlalchemy", ..., policy=MyPolicy())
    """

    def can_create(self, model: type, data: dict[str, Any], info: Any) -> bool:
        """Called before creating a new instance."""
        return True

    def can_update(
        self, model: type, instance: Any, data: dict[str, Any], info: Any
    ) -> bool:
        """Called before updating an existing instance."""
        return True

    def can_delete(self, model: type, instance: Any, info: Any) -> bool:
        """Called before deleting an instance."""
        return True

    def can_link(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
        """Called before adding an instance to a relation."""
        return True

    def can_unlink(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
        """Called before removing an instance from a relation."""
        return True

    def scope_query(self, model: type, query: Any, info: Any) -> Any:
        """Narrow object lookups to only rows the current user can access.

        Applied when loading objects by ID in mutations.  The *query*
        argument is a backend-native query object (Django ``QuerySet``,
        SQLAlchemy ``Select``, or Tortoise ``QuerySet``).
        """
        return query


def _check_policy(
    policy: MutationPolicy | None,
    method_name: str,
    *args: Any,
) -> None:
    """Call a policy method and raise ``PermissionError`` when it returns False."""
    if policy is None:
        return
    method = getattr(policy, method_name)
    if not method(*args):
        raise PermissionError(f"MutationPolicy.{method_name} denied the operation")


class _PolicyRepoDict(dict):
    """A dict subclass that returns the same repo class for *any* model key.

    Used internally to adapt a ``MutationPolicy`` (global) into the per-model
    ``repos`` dict expected by ``BaseBackend``.
    """

    def __init__(self, repo_cls: type) -> None:
        super().__init__()
        self._repo_cls = repo_cls

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        return self._repo_cls

    def __contains__(self, key: object) -> bool:
        return True


def _policy_to_repos(policy: MutationPolicy) -> _PolicyRepoDict:
    """Convert a deprecated ``MutationPolicy`` into a global repo dict.

    The resulting repo delegates every hook call to the original policy,
    translating between the per-model repo signatures and the global
    policy signatures (which include ``model`` as the first argument).
    """
    from strawberry_orm.repo import AbstractRepo

    warnings.warn(
        "MutationPolicy is deprecated. Use AbstractRepo and pass repos={} "
        "to StrawberryORM instead.",
        DeprecationWarning,
        stacklevel=3,
    )

    class _PolicyRepo(AbstractRepo):  # type: ignore[type-arg]
        """Auto-generated repo wrapping a MutationPolicy instance."""

        def can_create(self, data: dict[str, Any], info: Any) -> bool:
            return policy.can_create(self.model, data, info)

        def can_update(self, instance: Any, data: dict[str, Any], info: Any) -> bool:
            return policy.can_update(self.model, instance, data, info)

        def can_delete(self, instance: Any, info: Any) -> bool:
            return policy.can_delete(self.model, instance, info)

        def can_link(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
            return policy.can_link(parent, field, instance, info)

        def can_unlink(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
            return policy.can_unlink(parent, field, instance, info)

        def scope_query(self, query: Any, info: Any) -> Any:
            return policy.scope_query(self.model, query, info)

    return _PolicyRepoDict(_PolicyRepo)
