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

    orm = StrawberryORM.for_sqlalchemy(..., repos={Post: PostRepo})
"""

from __future__ import annotations

import inspect
import warnings
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
    #
    # A hook may be ``async`` where the write is: on an async SQLAlchemy
    # session and on Tortoise. A sync write refuses an awaitable with
    # ``TypeError`` rather than treat the coroutine as a truthy "allow".

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

    def can_upsert(self, instance: Any, info: Any) -> bool:
        """Called when an upsert matches *instance*, before :meth:`on_match`.

        A miss that goes on to create is checked by :meth:`can_create` instead.
        """
        return True

    # -- Upsert matching -----------------------------------------------------

    def find_for_upsert(self, model: type, query: dict[str, Any], info: Any) -> Any:
        """Return the row an upsert matches, or ``None``.

        *query* maps the sent ``where`` fields to their values; ``"id"`` is the
        primary key, already decoded from a Relay global ID. A nested upsert's
        query also carries the foreign key to the row it is nested under. The
        default looks the row up under :meth:`scope_query` and requires every
        field to match.
        """
        return self._backend._find_for_upsert(self, model, query, info)

    async def find_for_upsert_async(
        self, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        """Async variant of :meth:`find_for_upsert`.

        An override of :meth:`find_for_upsert` alone, sync or async, is used
        here too, so a repo only has to override one of them.
        """
        if type(self).find_for_upsert is not AbstractRepo.find_for_upsert:
            found = self.find_for_upsert(model, query, info)
            return await found if inspect.isawaitable(found) else found
        return await self._backend._find_for_upsert_async(self, model, query, info)

    def on_match(self, instance: Any, info: Any) -> Any:
        """Called once for every row an upsert matches, after authorization.

        May be ``async`` when the session is.
        """

    # -- Query scoping -------------------------------------------------------

    def scope_query(self, query: Any, info: Any) -> Any:
        """Narrow object lookups to only rows the current user may access.

        *query* is a backend-native query object (Django ``QuerySet``,
        SQLAlchemy ``Select``, or Tortoise ``QuerySet``). Narrow it with
        conditions, or limit it to none; any other limit or offset is refused,
        as a lookup takes its own.
        """
        return query

    # -- Lifecycle hooks (no-op by default) ----------------------------------
    #
    # Like the authorization hooks, these may be ``async`` where the write is,
    # and a sync write refuses an awaitable rather than skip the hook's body.

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
    #
    # Each ``*_async`` default calls an override of its sync counterpart, so a
    # repo has to override only one of them. On an async SQLAlchemy session
    # the override runs through ``AsyncSession.run_sync``, where the sync
    # defaults it calls through ``super()`` can do IO.

    async def _sync_override(self, name: str, info: Any, *args: Any) -> Any:
        method = getattr(self, name)
        backend = self._backend
        session = None
        if backend.__class__.__name__ == "SQLAlchemyBackend":
            session = backend._get_session(info)
        if session is not None and backend._is_async_session(session):
            result = await session.run_sync(lambda _sync_session: method(*args))
        else:
            result = method(*args)
        return await result if inspect.isawaitable(result) else result

    def _create(self, model: type, data: dict[str, Any], info: Any) -> Any:
        """Persist a new model instance and return it."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            if not model._meta.parents:
                return model.objects.create(**data)
            import django
            from django.db import models, router

            # ``objects.create()`` forces the insert into the model's own table
            # only, and saves a parent whose key is set with an UPDATE; before
            # Django 5.0, ``save()`` cannot force the parents' inserts either.
            if django.VERSION < (5, 0):  # pragma: no cover - Django < 5.0
                keys = sorted(
                    key for key in backend._row_keys(model) if data.get(key) is not None
                )
                if keys:
                    raise ValueError(
                        f"A {model.__name__} cannot be created with {keys} "
                        f"before Django 5.0: its parent rows would be updated"
                    )
            instance = model(**data)
            instance.save(
                force_insert=(models.Model,), using=router.db_for_write(model)
            )
            return instance

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _create_async instead"
            )

        session = _sa_sync_session(backend, info)
        instance = model(**data)
        session.add(instance)
        session.flush()
        return instance

    async def _create_async(self, model: type, data: dict[str, Any], info: Any) -> Any:
        """Async variant of :meth:`_create`."""
        if _overrides(self, "_create"):
            return await self._sync_override("_create", info, model, data, info)
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            return await model.create(**data)

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                instance = model(**data)
                session.add(instance)
                await session.flush()
                return instance

        return self._create(model, data, info)

    def _get(self, model: type, pk: Any, info: Any) -> Any | None:
        """Load an instance by primary key, respecting :meth:`scope_query`."""
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            qs = backend.call_repo_scope(self, model.objects.all(), info)
            return qs.filter(pk=pk).first()

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _get_async instead"
            )

        session = _sa_sync_session(backend, info)
        from sqlalchemy import select as sa_select

        stmt = sa_select(model).where(*_sa_key_is(model, pk))
        stmt = backend.call_repo_scope(self, stmt, info)
        result = session.execute(stmt)
        return result.scalars().first()

    async def _get_async(self, model: type, pk: Any, info: Any) -> Any | None:
        """Async variant of :meth:`_get`."""
        if _overrides(self, "_get"):
            return await self._sync_override("_get", info, model, pk, info)
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            qs = backend.call_repo_scope(self, model.all(), info)
            return await qs.filter(pk=pk).first()

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                from sqlalchemy import select as sa_select

                stmt = sa_select(model).where(*_sa_key_is(model, pk))
                stmt = backend.call_repo_scope(self, stmt, info)
                result = await session.execute(stmt)
                return result.scalars().first()

        return self._get(model, pk, info)

    def _save(self, instance: Any, info: Any) -> None:
        """Flush / save an already-tracked instance.

        A row a library write read is saved with an ``UPDATE`` of the
        attributes the write set on it, every column assigned on it since the
        read, and the columns the ORM stamps on each save, such as Django's
        ``auto_now``, and of nothing else; if no row has its key any more, the
        write is refused rather than the row inserted again. SQLAlchemy's unit
        of work does this itself. An override that assigns a column before
        calling this has it saved; one that saves the row itself has to do the
        same. Any other instance is saved as the ORM saves it.
        """
        backend = self._backend
        name = backend.__class__.__name__

        if name == "DjangoBackend":
            from strawberry_orm.backends.django import save_changes

            save_changes(instance)
            return

        if name == "TortoiseBackend":
            raise TypeError(
                "Tortoise backend is async-only; override _save_async instead"
            )

        session = _sa_sync_session(backend, info)
        from sqlalchemy.orm import object_session

        if object_session(instance) is None:
            session.add(instance)
        session.flush()

    async def _save_async(self, instance: Any, info: Any) -> None:
        """Async variant of :meth:`_save`."""
        if _overrides(self, "_save"):
            await self._sync_override("_save", info, instance, info)
            return
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            from strawberry_orm.backends.tortoise import save_changes

            await save_changes(instance)
            return

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                session.add(instance)
                await session.flush()
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

        session = _sa_sync_session(backend, info)
        session.delete(instance)
        session.flush()

    async def _delete_async(self, instance: Any, info: Any) -> None:
        """Async variant of :meth:`_delete`."""
        if _overrides(self, "_delete"):
            await self._sync_override("_delete", info, instance, info)
            return
        backend = self._backend
        name = backend.__class__.__name__

        if name == "TortoiseBackend":
            await instance.delete()
            return

        if name == "SQLAlchemyBackend":
            session = backend._get_session(info)
            if backend._is_async_session(session):
                await session.delete(instance)
                await session.flush()
                return

        self._delete(instance, info)


def _overrides(repo: AbstractRepo, name: str) -> bool:
    return getattr(type(repo), name) is not getattr(AbstractRepo, name)


def _sa_sync_session(backend: Any, info: Any) -> Any:
    """The session a sync SQLAlchemy default works in.

    An async session's sync session does IO only inside ``run_sync``, so a
    sync default reached any other way on one raises rather than do nothing.
    """
    session = backend._get_session(info)
    if not backend._is_async_session(session):
        return session
    from sqlalchemy.util.concurrency import in_greenlet

    if not in_greenlet():
        raise TypeError(
            "A sync AbstractRepo method was called with an AsyncSession; call "
            "its *_async variant, or reach it through one"
        )
    return session.sync_session


def _allows(result: Any, check: str) -> bool:
    """Whether a permission check's *result* allows the operation.

    ``True`` allows; ``False`` and ``None`` deny. Any other truthy value still
    allows, with a ``DeprecationWarning``, so a hook that returns a row or a
    count keeps working until it is changed to return a bool.
    """
    if result is True:
        return True
    if not result:
        return False
    warnings.warn(
        f"{check} returned {type(result).__name__}, not a bool. Return True to "
        f"allow the operation; a later release will treat anything else as a "
        f"denial.",
        DeprecationWarning,
        stacklevel=2,
    )
    return True


def _require(result: Any, check: str) -> None:
    if not _allows(result, check):
        raise PermissionError(f"{check} denied the operation")


def _get_sa_pk_column(model: type) -> Any:
    """Return the primary key column for a SQLAlchemy model.

    A key of several columns is refused: one of them alone matches every row
    that shares it.
    """
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    pk_cols = mapper.primary_key
    if len(pk_cols) != 1:
        raise ValueError(
            f"Model {model.__name__} has {len(pk_cols)} primary key columns; "
            "expected exactly 1"
        )
    return pk_cols[0]


def _sa_key_is(model: type, pk: Any) -> list[Any]:
    """The conditions that a SQLAlchemy *model* row's primary key is *pk*.

    A key of several columns is the tuple of their values, as ``pk_value``
    reads it off a row; one value cannot name a row by it.
    """
    from sqlalchemy import inspect as sa_inspect

    columns = list(sa_inspect(model).primary_key)
    if len(columns) == 1:
        return [columns[0] == pk]
    if not isinstance(pk, tuple) or len(pk) != len(columns):
        raise ValueError(
            f"Model {model.__name__} has {len(columns)} primary key columns, "
            f"so its key is a tuple of {len(columns)} values"
        )
    return [column == value for column, value in zip(columns, pk, strict=True)]
