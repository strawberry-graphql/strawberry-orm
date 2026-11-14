"""What a ``groupBy`` asks for, worked out once for every backend.

A group-by entry names a column of the model (``field:``), a column of a
related model reached through ``object:``, the set of a to-many relation's
rows (``objects:``), or a ``@group_field`` method. Each becomes a
:class:`GroupPath`. The backends turn paths into expressions and
hand back one value per path; the key a group reports, and the edges it
claims, are built from those values here.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import strawberry

from strawberry_orm.types import DateGroupByInterval

__all__ = [
    "SQLITE_PERIOD_START",
    "GroupHop",
    "GroupPath",
    "assign_edge_indices",
    "build_group",
    "format_group_value",
    "from_json",
    "group_paths",
    "key_tuple",
    "needs_database_membership",
]

#: SQLite date modifiers that move a value to the start of its period. A
#: quarter needs the month read first, so each backend builds its own.
SQLITE_PERIOD_START = {
    DateGroupByInterval.DAY: ("start of day",),
    # 'weekday 0' moves forward to the next Sunday unless it is one already;
    # six days back from there is the Monday the ISO week starts on.
    DateGroupByInterval.WEEK: ("start of day", "weekday 0", "-6 days"),
    DateGroupByInterval.MONTH: ("start of month",),
    DateGroupByInterval.YEAR: ("start of year",),
}


@dataclass(frozen=True)
class GroupHop:
    """One relation a path crosses: ``relation`` on ``model``, to ``target``."""

    relation: str
    model: type
    target: type
    many: bool


@dataclass(frozen=True)
class GroupPath:
    """One value a group is keyed by.

    ``alias`` names the value in the backend's query. ``name`` is the key
    field it is reported under, inside one nested key per hop.
    """

    alias: str
    name: str
    hops: tuple[GroupHop, ...] = ()
    column: str | None = None
    interval: DateGroupByInterval | None = None
    custom: Callable[..., Any] | None = None
    #: The ``@group_field`` method's input instance and the value it was given.
    owner: Any = None
    value: Any = None
    #: Under ``objects:``, the to-many relation whose rows are grouped by
    #: together, and the columns of theirs the key holds, with the interval
    #: each date is cut to and what reads each value back out of JSON.
    through: GroupHop | None = None
    columns: tuple[str, ...] = ()
    intervals: tuple[DateGroupByInterval | None, ...] = ()
    readers: tuple[Callable[[Any], Any], ...] = ()

    @property
    def key_path(self) -> tuple[str, ...]:
        return (*(hop.relation for hop in self.hops), self.name)

    @property
    def model(self) -> type | None:
        """The model *column* belongs to, when the path crosses a relation."""
        return self.hops[-1].target if self.hops else None


def _is_set(value: Any) -> bool:
    return value is not strawberry.UNSET and value is not None and value is not False


def group_paths(backend: Any, model: type, group_by: Any) -> list[GroupPath]:
    """Every value *group_by* groups *model* by, in the order it names them.

    A value named twice is grouped by once. A to-many relation can only be
    the first hop of a path: past a to-one hop it would multiply rows the
    database has no single row to collapse them back into.
    """
    entries = group_by if isinstance(group_by, list) else [group_by]
    found: dict[tuple[str, ...], GroupPath] = {}
    for entry in entries:
        _collect(backend, model, entry, (), found)
    return [replace(path, alias=f"_g{i}") for i, path in enumerate(found.values())]


def _collect(
    backend: Any,
    model: type,
    entry: Any,
    hops: tuple[GroupHop, ...],
    found: dict[tuple[str, ...], GroupPath],
) -> None:
    if not _is_set(entry):
        return
    custom = getattr(type(entry), "_custom_groups", {})
    for key in entry.__class__.__dataclass_fields__:
        value = getattr(entry, key)
        if not _is_set(value):
            continue
        if key == "field":
            for column in value.__class__.__dataclass_fields__:
                option = getattr(value, column)
                if not _is_set(option):
                    continue
                path = GroupPath(
                    alias="",
                    name=column,
                    hops=hops,
                    column=column,
                    interval=getattr(option, "interval", None),
                )
                found.setdefault(path.key_path, path)
        elif key == "object":
            for relation in value.__class__.__dataclass_fields__:
                nested = getattr(value, relation)
                if not _is_set(nested):
                    continue
                hop = backend.relation_hop(model, relation)
                if hop.many and hops:
                    raise ValueError(
                        f"Cannot group by {model.__name__}.{relation} through "
                        f"{'.'.join(h.relation for h in hops)}: a to-many relation "
                        f"can only be grouped by directly on the model."
                    )
                _collect(backend, hop.target, nested, (*hops, hop), found)
        elif key == "objects":
            for relation in value.__class__.__dataclass_fields__:
                nested = getattr(value, relation)
                if _is_set(nested):
                    _collect_together(backend, model, relation, nested, hops, found)
        elif key in custom:
            if hops:
                raise ValueError(
                    f"@group_field {key} groups {model.__name__} itself and "
                    f"cannot be reached through object:."
                )
            path = GroupPath(
                alias="",
                name=key,
                custom=custom[key],
                owner=entry,
                value=value,
            )
            found.setdefault(path.key_path, path)


def _collect_together(
    backend: Any,
    model: type,
    relation: str,
    entry: Any,
    hops: tuple[GroupHop, ...],
    found: dict[tuple[str, ...], GroupPath],
) -> None:
    """The path ``objects: {relation: entry}`` groups *model* by.

    Every column *entry* names goes into one value, so a row's related rows
    are told apart by all of them at once. Naming the relation again adds
    columns to the same value.
    """
    hop = backend.relation_hop(model, relation)
    where = f"objects: {{{relation}}} on {model.__name__}"
    if hops or not hop.many:
        raise ValueError(
            f"Cannot group by {where}: objects: groups a model by the rows of "
            f"one of its own to-many relations."
        )
    path = found.get(("objects", relation))
    columns: dict[str, DateGroupByInterval | None] = (
        dict(zip(path.columns, path.intervals, strict=True)) if path else {}
    )
    for key in entry.__class__.__dataclass_fields__:
        value = getattr(entry, key)
        if not _is_set(value):
            continue
        if key != "field":
            raise ValueError(
                f"Cannot group by {where} through {key}: the related rows are "
                f"grouped by their own columns, under field:."
            )
        for column in value.__class__.__dataclass_fields__:
            option = getattr(value, column)
            if _is_set(option):
                columns.setdefault(column, getattr(option, "interval", None))
    if not columns:
        return
    found[("objects", relation)] = GroupPath(
        alias="",
        name=relation,
        through=hop,
        columns=tuple(columns),
        intervals=tuple(columns.values()),
        readers=tuple(
            backend.together_reader(hop.target, column, interval)
            for column, interval in columns.items()
        ),
    )


def from_json(value: Any, kind: Any) -> Any:
    """*value*, as JSON gave it, read as the column's grouped value reads.

    JSON has no dates, UUIDs or booleans of its own: each database writes
    them as it stores them, a string or a number.
    """
    if value is None or kind is None:
        return value
    if kind is bool:
        return bool(value)
    if kind in (float, int) and not isinstance(value, str):
        return kind(value)
    if not isinstance(value, str):
        return value
    if kind is uuid.UUID:
        return uuid.UUID(value)
    if kind is datetime.datetime:
        return datetime.datetime.fromisoformat(value)
    if kind is datetime.date:
        # A date cut to its interval may come back as the instant it starts.
        return datetime.datetime.fromisoformat(value).date()
    if kind is datetime.time:
        return datetime.time.fromisoformat(value)
    return value


def _together_key(meta: Any, path: GroupPath, value: str) -> list[Any]:
    """The related keys *value*, a JSON array of column values per row, holds."""
    key_type = meta.relation_keys[path.name].group_key_type
    return [
        key_type(
            **{
                column: format_group_value(read(item), interval)
                for column, read, interval, item in zip(
                    path.columns, path.readers, path.intervals, row, strict=True
                )
            }
        )
        for row in json.loads(value, parse_float=Decimal)
    ]


def format_group_value(value: Any, interval: DateGroupByInterval | None) -> Any:
    """A key value as the client reads it: a string, or ``None``.

    A truncated date reads as the ISO form of the period's first instant.
    """
    if value is None:
        return None
    if interval is not None and isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    return str(value)


def key_tuple(paths: Iterable[GroupPath], values: Mapping[str, Any]) -> tuple:
    """The formatted values *values* holds for *paths*, in path order.

    The rows ``objects:`` groups by together are told apart by the JSON text
    the database wrote for them, ``[]`` for none.
    """
    return tuple(
        (values[p.alias] or "[]")
        if p.through is not None
        else format_group_value(values[p.alias], p.interval)
        for p in paths
    )


def _build_key(meta: Any, paths: list[GroupPath], formatted: tuple) -> Any:
    tree: dict[str, Any] = {}
    together: dict[str, list[Any]] = {}
    for path, value in zip(paths, formatted, strict=True):
        if path.through is not None:
            together[path.name] = _together_key(meta, path, value)
            continue
        node = tree
        for hop in path.hops:
            node = node.setdefault(hop.relation, {})
        node[path.name] = value
    key = _instantiate(meta, tree)
    if together:
        key.objects = meta.objects_key_type(**together)
    return key


def _instantiate(meta: Any, tree: dict[str, Any]) -> Any:
    kwargs = {
        name: _instantiate(meta.relation_keys[name], value)
        if isinstance(value, dict)
        else value
        for name, value in tree.items()
    }
    return meta.group_key_type(**kwargs)


class Group:
    """One group, before the connection hangs its edges and items off it."""

    def __init__(self, key: Any, aggregates: Any, key_values: tuple) -> None:
        self.key = key
        self.aggregates = aggregates
        self.edge_indices: list[int] = []
        self._orm_key_values = key_values
        self._items_nodes: list[Any] | None = None
        self._items_offset = 0


def build_group(
    meta: Any,
    paths: list[GroupPath],
    values: Mapping[str, Any],
    requested: dict[str, Any],
) -> Group:
    """A group from one row of the grouped query, read through *values*."""
    formatted = key_tuple(paths, values)
    row = type("Row", (), dict(values))()
    return Group(
        _build_key(meta, paths, formatted),
        meta.build_aggregates(row, requested),
        formatted,
    )


def needs_database_membership(paths: list[GroupPath]) -> bool:
    """True when an edge's own attributes cannot say which groups it is in.

    A related column, a truncated date, a ``@group_field`` and rows grouped
    by together are worked out by the database, so the database is asked for
    each edge's keys.
    """
    return any(p.hops or p.interval is not None or p.custom or p.through for p in paths)


def own_key_values(node: Any, paths: list[GroupPath]) -> tuple:
    """The key an edge's row is grouped under, read off the row itself."""
    return tuple(format_group_value(getattr(node, p.name, None), None) for p in paths)


def assign_edge_indices(
    groups: list[Any], memberships: list[set[tuple]] | list[tuple]
) -> None:
    """Point each group at the edges whose keys include its own.

    *memberships* holds, per edge, its key or the set of keys it is grouped
    under (one per related row of a to-many path). A ``None`` in a key matches
    only a ``None``: the null group holds the rows with no value, not every row.
    """
    by_key: dict[tuple, list[int]] = {}
    for index, keys in enumerate(memberships):
        for key in keys if isinstance(keys, set) else (keys,):
            by_key.setdefault(key, []).append(index)
    for group in groups:
        group.edge_indices = by_key.get(group._orm_key_values, [])
