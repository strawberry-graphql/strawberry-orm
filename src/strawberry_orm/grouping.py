"""What a ``groupBy`` asks for, worked out once for every backend.

A group-by entry names a column of the model (``field:``), a column of a
related model reached through ``object:``, the set of a to-many relation's
rows (``objects:``), or a ``@group_field`` method. Each becomes a
:class:`GroupPath`. The backends turn paths into expressions and
hand back one value per path; the key a group reports, and the edges it
claims, are built from those values here.

An order entry can name the set of a to-many relation's rows too. It becomes
a :class:`SetOrder` over the path ``objects:`` grouping reads, so a row sorts
by the value its group is keyed by.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import strawberry

from strawberry_orm.types import DateGroupByInterval

__all__ = [
    "MAX_GROUPS",
    "MAX_GROUP_BY_ENTRIES",
    "MAX_GROUP_KEY_ROWS",
    "MAX_ORDER_ENTRIES",
    "SQLITE_PERIOD_START",
    "GroupHop",
    "GroupPath",
    "SetOrder",
    "assign_edge_indices",
    "build_group",
    "format_group_value",
    "from_json",
    "group_paths",
    "key_tuple",
    "needs_database_membership",
    "order_terms",
    "within_max_groups",
]

#: The defaults of ``max_order_entries`` and ``max_group_by_entries``: how many
#: entries an ``order`` or ``groupBy`` list may have.
MAX_ORDER_ENTRIES = 10
MAX_GROUP_BY_ENTRIES = 10
#: The default of ``max_groups``: how many groups one connection reports.
MAX_GROUPS = 100
#: The default of ``max_group_key_rows``: how many related rows one
#: ``objects:`` group key may hold.
MAX_GROUP_KEY_ROWS = 100

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


def _entries(argument: str, given: Any, maximum: int) -> list[Any]:
    """The entries of the *argument* list *given*, refused past *maximum*.

    Each entry becomes a clause, and a join for each relation it crosses, so
    the length is checked before any of them is built.
    """
    entries = given if isinstance(given, list) else [given]
    if len(entries) > maximum:
        raise ValueError(f"{argument} has {len(entries)} entries; maximum is {maximum}")
    return entries


def group_paths(backend: Any, model: type, group_by: Any) -> list[GroupPath]:
    """Every value *group_by* groups *model* by, in the order it names them.

    A value named twice is grouped by once. A to-many relation can only be
    the first hop of a path: past a to-one hop it would multiply rows the
    database has no single row to collapse them back into.
    """
    entries = _entries(
        "groupBy",
        group_by,
        getattr(backend, "_max_group_by_entries", MAX_GROUP_BY_ENTRIES),
    )
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


@dataclass(frozen=True)
class SetOrder:
    """An ``objects:`` order clause: rows by the set of their related rows.

    *path* is read as ``objects:`` grouping reads it, into the same JSON
    text. That text is never null, ``[]`` for a row with none, so of the
    ordering only its direction counts.
    """

    path: GroupPath
    descending: bool


def order_terms(backend: Any, model: type, order_input: Any) -> list[Any]:
    """*order_input*'s entries, each after the ``objects:`` clauses it starts.

    The entries are ordered by as they always were: an entry's ``objects:``
    is left to its :class:`SetOrder`. A relation named in several entries
    is ordered by once, where it is first named, by every column they name,
    in the order they name them: the set a ``groupBy`` naming those columns
    keys groups by.
    """
    entries = _entries(
        "order",
        order_input,
        getattr(backend, "_max_order_entries", MAX_ORDER_ENTRIES),
    )
    sets: dict[str, SetOrder] = {}
    columns: dict[str, list[str]] = {}
    starts: dict[int, list[str]] = {}
    for index, entry in enumerate(entries):
        _refuse_sets_through(entry, ())
        objects = getattr(entry, "objects", None)
        if not _is_set(objects):
            continue
        for relation in objects.__class__.__dataclass_fields__:
            nested = getattr(objects, relation)
            if not _is_set(nested):
                continue
            hop = backend.relation_hop(model, relation)
            where = f"objects: {{{relation}}} on {model.__name__}"
            if not hop.many:
                raise ValueError(
                    f"Cannot order by {where}: objects: orders a model by the "
                    f"rows of one of its own to-many relations."
                )
            for column, desc in _set_columns(where, nested):
                if relation not in sets:
                    path = GroupPath(alias=f"_s{len(sets)}", name=relation, through=hop)
                    sets[relation] = SetOrder(path, desc)
                    columns[relation] = []
                    starts.setdefault(index, []).append(relation)
                elif sets[relation].descending != desc:
                    raise ValueError(
                        f"Cannot order by {where} both ascending and "
                        f"descending: its rows make one value, which sorts one "
                        f"way."
                    )
                if column not in columns[relation]:
                    columns[relation].append(column)
    terms: list[Any] = []
    for index, entry in enumerate(entries):
        for relation in starts.get(index, ()):
            named = tuple(columns[relation])
            path = replace(
                sets[relation].path, columns=named, intervals=(None,) * len(named)
            )
            terms.append(replace(sets[relation], path=path))
        terms.append(entry)
    return terms


def _set_columns(where: str, entry: Any) -> list[tuple[str, bool]]:
    """The columns *entry* names under ``objects:``, each with its direction."""
    found: list[tuple[str, bool]] = []
    for key in entry.__class__.__dataclass_fields__:
        value = getattr(entry, key)
        if not _is_set(value):
            continue
        if key != "field":
            raise ValueError(
                f"Cannot order by {where} through {key}: the related rows are "
                f"ordered by their own columns, under field:."
            )
        for column in value.__class__.__dataclass_fields__:
            direction = getattr(value, column)
            if _is_set(direction):
                name = getattr(direction, "value", str(direction))
                found.append((column, name.startswith("DESC")))
    return found


def _refuse_sets_through(entry: Any, hops: tuple[str, ...]) -> None:
    """Refuse ``objects:`` reached through ``object:``, as grouping does."""
    for key in entry.__class__.__dataclass_fields__:
        value = getattr(entry, key)
        if not _is_set(value):
            continue
        if key == "objects" and hops:
            relations = [
                relation
                for relation in value.__class__.__dataclass_fields__
                if _is_set(getattr(value, relation))
            ]
            raise ValueError(
                f"Cannot order by objects: {{{', '.join(relations)}}} through "
                f"{'.'.join(hops)}: objects: orders a model by the rows of one "
                f"of its own to-many relations."
            )
        if key == "object":
            for relation in value.__class__.__dataclass_fields__:
                nested = getattr(value, relation)
                if _is_set(nested):
                    _refuse_sets_through(nested, (*hops, relation))


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


def _together_key(meta: Any, path: GroupPath, value: str, max_rows: int) -> list[Any]:
    """The related keys *value*, a JSON array of column values per row, holds."""
    key_type = meta.relation_keys[path.name].group_key_type
    rows = json.loads(value, parse_float=Decimal)
    if len(rows) > max_rows:
        raise ValueError(
            f"objects: puts more than {max_rows} related rows in one group key"
        )
    return [
        key_type(
            **{
                column: format_group_value(read(item), interval)
                for column, read, interval, item in zip(
                    path.columns, path.readers, path.intervals, row, strict=True
                )
            }
        )
        for row in rows
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


def _build_key(
    meta: Any, paths: list[GroupPath], formatted: tuple, max_key_rows: int
) -> Any:
    tree: dict[str, Any] = {}
    together: dict[str, list[Any]] = {}
    for path, value in zip(paths, formatted, strict=True):
        if path.through is not None:
            together[path.name] = _together_key(meta, path, value, max_key_rows)
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
    *,
    max_key_rows: int = MAX_GROUP_KEY_ROWS,
) -> Group:
    """A group from one row of the grouped query, read through *values*.

    An ``objects:`` key holding more than *max_key_rows* related rows is
    refused.
    """
    formatted = key_tuple(paths, values)
    row = type("Row", (), dict(values))()
    return Group(
        _build_key(meta, paths, formatted, max_key_rows),
        meta.build_aggregates(row, requested),
        formatted,
    )


def within_max_groups[T](rows: Sequence[T], max_groups: int) -> Sequence[T]:
    """*rows*, one per group, refused when there are more than *max_groups*.

    The grouped query asks for one row past the cap, so a grouping over it
    is found without reading every group.
    """
    if len(rows) > max_groups:
        raise ValueError(
            f"groupBy makes more than {max_groups} groups; filter the "
            f"connection or group by fewer values"
        )
    return rows


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
