"""Per-model repository base class for strawberry-orm.

``AbstractRepo[M]`` centralises authorization, query scoping, lifecycle
hooks, and overridable CRUD for a single ORM model.  Register repo
classes when constructing :class:`StrawberryORM` to have auto-generated
mutations delegate to them.

Usage::

    class PostRepo(AbstractRepo[Post]):
        def scope_query(self, query, info):
            return query.filter(tenant_id=info.context["tenant_id"])

        def can_update(self, instance, data, info):
            return instance.author_id == info.context["user"].id

    orm = StrawberryORM("sqlalchemy", ..., repos={Post: PostRepo})
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar, get_args

try:
    from typing import get_original_bases
except ImportError:
    from types import get_original_bases

M = TypeVar("M")


class AbstractRepo(Generic[M]):
    """Base repository for a single ORM model.

    Subclass this and override hooks as needed.  The ``model`` class
    attribute is automatically extracted from the generic parameter
    (e.g. ``class PostRepo(AbstractRepo[Post])`` sets ``model = Post``),
    or you can set it explicitly.

    At runtime an instance receives *backend* and can use it to dispatch
    CRUD to the correct ORM engine.
    """

    model: type | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.model is not None:
            return
        for base in get_original_bases(cls):
            origin = getattr(base, "__origin__", None)
            if origin is AbstractRepo:
                args = get_args(base)
                if args and not isinstance(args[0], TypeVar):
                    cls.model = args[0]
                    break

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    # -- Authorization hooks (return True to allow, False to deny) -----------

    def can_create(self, data: dict[str, Any], info: Any) -> bool:
        """Called before creating a new instance."""
        return True

    def can_update(self, instance: Any, data: dict[str, Any], info: Any) -> bool:
        """Called before updating an existing instance."""
        return True

    def can_delete(self, instance: Any, info: Any) -> bool:
        """Called before deleting an instance."""
        return True

    def can_link(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
        """Called before adding an instance to a relation."""
        return True

    def can_unlink(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
        """Called before removing an instance from a relation."""
        return True

    # -- Query scoping -------------------------------------------------------

    def scope_query(self, query: Any, info: Any) -> Any:
        """Narrow object lookups to only rows the current user may access.

        *query* is a backend-native query object (Django ``QuerySet``,
        SQLAlchemy ``Select``, or Tortoise ``QuerySet``).
        """
        return query

    # -- Lifecycle hooks (no-op by default) ----------------------------------

    def on_before_create(self, data: dict[str, Any], info: Any) -> dict[str, Any]:
        """Transform / augment *data* before a new instance is persisted."""
        return data

    def on_after_create(self, instance: Any, info: Any) -> None:
        """Called after the instance has been flushed / saved."""

    def on_before_update(
        self, instance: Any, data: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        """Transform *data* before fields are set on the instance."""
        return data

    def on_after_update(self, instance: Any, info: Any) -> None:
        """Called after the instance has been saved."""

    def on_before_delete(self, instance: Any, info: Any) -> None:
        """Called just before the instance is deleted."""

    # -- Overridable CRUD (backend-aware defaults) ---------------------------

    def _create(self, model: type, data: dict[str, Any], info: Any) -> Any:
        """Persist a new model instance and return it."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            return model.objects.create(**data)

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _create_async instead"
            )

        session = backend._get_session(info)
        instance = model(**data)
        session.add(instance)
        session.flush()
        return instance

    async def _create_async(self, model: type, data: dict[str, Any], info: Any) -> Any:
        """Async variant of :meth:`_create`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            return await model.create(**data)

        return self._create(model, data, info)

    def _get(self, model: type, pk: Any, info: Any) -> Any | None:
        """Load an instance by primary key, respecting :meth:`scope_query`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            qs = model.objects.all()
            qs = self.scope_query(qs, info)
            return qs.filter(pk=pk).first()

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _get_async instead"
            )

        session = backend._get_session(info)
        from sqlalchemy import select as sa_select

        pk_col = _get_sa_pk_column(model)
        stmt = sa_select(model).where(pk_col == pk)
        stmt = self.scope_query(stmt, info)
        result = session.execute(stmt)
        return result.scalars().first()

    async def _get_async(self, model: type, pk: Any, info: Any) -> Any | None:
        """Async variant of :meth:`_get`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            qs = model.all()
            qs = self.scope_query(qs, info)
            return await qs.filter(pk=int(pk)).first()

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                from sqlalchemy import select as sa_select

                pk_col = _get_sa_pk_column(model)
                stmt = sa_select(model).where(pk_col == pk)
                stmt = self.scope_query(stmt, info)
                result = await session.execute(stmt)
                return result.scalars().first()

        return self._get(model, pk, info)

    def _save(self, instance: Any, info: Any) -> None:
        """Flush / save an already-tracked instance."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            instance.save()
            return

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _save_async instead"
            )

        session = backend._get_session(info)
        from sqlalchemy.orm import object_session

        if object_session(instance) is None:
            session.add(instance)
        session.flush()

    async def _save_async(self, instance: Any, info: Any) -> None:
        """Async variant of :meth:`_save`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            await instance.save()
            return

        self._save(instance, info)

    def _delete(self, instance: Any, info: Any) -> None:
        """Remove the instance from the database."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            instance.delete()
            return

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _delete_async instead"
            )

        session = backend._get_session(info)
        session.delete(instance)
        session.flush()

    async def _delete_async(self, instance: Any, info: Any) -> None:
        """Async variant of :meth:`_delete`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            await instance.delete()
            return

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                await session.delete(instance)
                return

        self._delete(instance, info)


def _check_auth(
    repo: AbstractRepo | None,
    method_name: str,
    *args: Any,
) -> None:
    """Call an authorization method on a repo and raise on denial."""
    if repo is None:
        return
    method = getattr(repo, method_name)
    if not method(*args):
        raise PermissionError(f"AbstractRepo.{method_name} denied the operation")


def _get_sa_pk_column(model: type) -> Any:
    """Return the primary key column for a SQLAlchemy model."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    pk_cols = mapper.primary_key
    if len(pk_cols) != 1:
        raise ValueError(
            f"Model {model.__name__} has {len(pk_cols)} primary key columns; "
            "expected exactly 1"
        )
    return pk_cols[0]
