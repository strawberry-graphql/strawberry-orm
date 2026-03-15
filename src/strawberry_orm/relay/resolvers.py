"""Non-Django node resolution helpers (shared by SQLAlchemy + Tortoise)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from strawberry_orm._async import run_sync

if TYPE_CHECKING:
    from strawberry.types import Info


async def resolve_nodes_sqlalchemy(
    model: type,
    *,
    info: Info,
    node_ids: Iterable[str],
    required: bool = False,
) -> list[Any]:
    """Resolve nodes by querying an SQLAlchemy session.

    Expects ``info.context`` to have a ``session`` attribute (or a
    ``get_session`` callable).
    """
    from sqlalchemy import select

    session = _get_session(info)
    pk_col = _get_pk_column(model)
    ids = list(node_ids)

    stmt = select(model).where(pk_col.in_(ids))
    if _is_async_session(session):
        result = await session.execute(stmt)
        rows = {str(getattr(r, pk_col.key)): r for r in result.scalars().all()}
    else:
        rows = await run_sync(
            _resolve_nodes_sync,
            session,
            stmt,
            pk_col.key,
            thread_sensitive=True,
        )

    nodes: list[Any] = []
    for nid in ids:
        node = rows.get(nid)
        if node is None and required:
            raise ValueError(f"{model.__name__} with id {nid!r} not found")
        nodes.append(node)
    return nodes


async def resolve_nodes_tortoise(
    model: type,
    *,
    info: Any,
    node_ids: Iterable[str],
    required: bool = False,
) -> list[Any]:
    """Resolve nodes by querying Tortoise ORM."""
    ids = list(node_ids)
    pk_field = _get_tortoise_pk_field(model)
    rows_qs = model.filter(**{f"{pk_field}__in": ids})
    rows_list = await rows_qs
    rows = {str(getattr(r, pk_field)): r for r in rows_list}

    nodes: list[Any] = []
    for nid in ids:
        node = rows.get(nid)
        if node is None and required:
            raise ValueError(f"{model.__name__} with id {nid!r} not found")
        nodes.append(node)
    return nodes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_session(info: Any) -> Any:
    ctx = info.context
    if hasattr(ctx, "session"):
        session = ctx.session
        return session() if callable(session) else session
    if hasattr(ctx, "get_session"):
        return ctx.get_session()
    raise RuntimeError(
        "SQLAlchemy backend requires info.context.session or info.context.get_session"
    )


def _is_async_session(session: Any) -> bool:
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
    except ImportError:
        return False

    return isinstance(session, AsyncSession)


def _resolve_nodes_sync(session: Any, stmt: Any, pk_key: str) -> dict[str, Any]:
    result = session.execute(stmt)
    return {str(getattr(row, pk_key)): row for row in result.scalars().all()}


def _get_pk_column(model: type) -> Any:
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    pk_cols = mapper.primary_key
    if len(pk_cols) != 1:
        raise NotImplementedError("Composite primary keys are not supported")
    return pk_cols[0]


def _get_tortoise_pk_field(model: type) -> str:
    meta = model._meta  # type: ignore[attr-defined]
    return meta.pk_attr
