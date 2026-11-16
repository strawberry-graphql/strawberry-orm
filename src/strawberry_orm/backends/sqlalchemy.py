"""SQLAlchemy backend -- built from scratch using SQLAlchemy introspection."""

from __future__ import annotations

import datetime
import functools
import sys
import uuid
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import strawberry
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm.state import InstanceState
from sqlalchemy.sql import Select
from strawberry.extensions import SchemaExtension

from strawberry_orm._async import run_orm_work
from strawberry_orm.backends._base import (
    _AGGREGATES,
    _SENSITIVE_PATTERNS,
    WRITE_REFUSED,
    AggregateMeta,
    BaseBackend,
    extract_element_type,
    hint_hops,
    invoke_custom_callback,
    model_is_type_of,
    past_a_hop,
    requested_aggregates,
)
from strawberry_orm.backends.filter_budget import check_filter_budget
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
)
from strawberry_orm.fields import call_scope
from strawberry_orm.filters import is_fk_shortcut_lookup, is_reference_lookup
from strawberry_orm.lazy_resolution import reads_relation
from strawberry_orm.optimizer import OptimizerExtension
from strawberry_orm.types import DateGroupByInterval

#: Relation-name sets by model class; see :meth:`SQLAlchemyBackend.relation_names`.
_RELATION_NAMES: dict[type, set[str]] = {}
#: Column types PostgreSQL has no ``MIN`` or ``MAX`` of, even cast.
_SA_UNRANKABLE_TYPES = frozenset(
    {"JSON", "LargeBinary", "HSTORE", "TSVECTOR", "Geometry", "Geography", "Raster"}
)


Steps = Generator[Any, Any, Any]


class _PlannedLoad:
    """One relation in a query's load plan, with the relations under it.

    A relation that joins on one column a side has ``keys`` (the parent's
    attribute, the related row's column, and that column's attribute), and
    through an association table, ``link``: the table's column for each side.
    It can be read by a statement of its own: ``scope`` narrowed to the
    parents' keys. A to-many relation whose scope orders (``ordered``) is
    always read that way, and so is one whose scope says more than a loader's
    criteria can (``whole``). ``token`` is the edge's own ``scope=``, which
    its relation cache is marked with.
    """

    __slots__ = (
        "attr",
        "uselist",
        "mapper",
        "criteria",
        "scope",
        "token",
        "ordered",
        "whole",
        "keys",
        "link",
        "children",
    )

    def __init__(self, attr: Any, uselist: bool, mapper: Any, criteria: Any) -> None:
        self.attr = attr
        self.uselist = uselist
        self.mapper = mapper
        self.criteria = criteria
        self.scope: Callable[[Any], Any] | None = None
        self.token: Any = None
        self.ordered = False
        self.whole = False
        self.keys: tuple[str, Any, str] | None = None
        self.link: tuple[Any, Any] | None = None
        self.children: dict[str, _PlannedLoad] = {}

    @property
    def scoped(self) -> bool:
        return self.criteria is not None or self.scope is not None

    def reads_alone(self, every: bool = False) -> bool:
        """Whether a statement of its own reads it, not a loader option.

        With *every*, so does each to-many relation keyed on the related row.
        """
        return (
            self.ordered
            or self.whole
            or (every and self.uselist and self.keys is not None and self.link is None)
        )


def _loader_options(level: dict[str, _PlannedLoad], parent: Any = None) -> list[Any]:
    """One loader option per leaf; siblings branch from the same parent loader.

    A relation read by a statement of its own gets none, nor does what is
    below it: that statement carries those.
    """
    from sqlalchemy.orm import joinedload, selectinload

    chains: list[Any] = []
    for load in level.values():
        if load.reads_alone():
            continue
        attr = load.attr if load.criteria is None else load.attr.and_(load.criteria)
        if parent is None:
            loader = (selectinload if load.uselist else joinedload)(attr)
        else:
            loader = (parent.selectinload if load.uselist else parent.joinedload)(attr)
        chains.extend(_loader_options(load.children, loader) or [loader])
    return chains


_KEYS_PER_STATEMENT = 500

#: Stands for the scope a planned relation is loaded with when its load does
#: not apply that scope, so a cache it fills is never marked as scoped.
_UNMARKED = object()


def _equates(join: Any, left: Any, right: Any) -> bool:
    return (left == right).compare(join) or (right == left).compare(join)


def _join_keys(rel: Any) -> tuple[Any, Any, tuple[Any, Any] | None] | None:
    """The columns a relation joins on, if that is all its join says.

    ``(the parent's column, the related row's column, link)``, where ``link``
    is ``None`` unless the relation goes through an association table, and
    then that table's columns for each. A relation whose join says more
    (``viewonly`` with a filter in its ``primaryjoin``) has none: only a
    loader reads the whole join.
    """
    if rel.secondary is None:
        pairs = list(rel.local_remote_pairs or ())
        if len(pairs) == 1 and _equates(rel.primaryjoin, *pairs[0]):
            return pairs[0][0], pairs[0][1], None
        return None
    if len(rel.synchronize_pairs) != 1 or len(rel.secondary_synchronize_pairs) != 1:
        return None
    parent, parent_link = rel.synchronize_pairs[0]
    related, related_link = rel.secondary_synchronize_pairs[0]
    if _equates(rel.primaryjoin, parent, parent_link) and _equates(
        rel.secondaryjoin, related, related_link
    ):
        return parent, related, (parent_link, related_link)
    return None


def _marks_some(edges: list[tuple[type, str, Any]]) -> bool:
    return any(token is not _UNMARKED for _model, _name, token in edges)


def _reads_alone_below(level: dict[str, _PlannedLoad]) -> bool:
    return any(
        load.reads_alone() or _reads_alone_below(load.children)
        for load in level.values()
    )


def _related_rows(rows: list[Any], name: str) -> list[Any]:
    """The rows *name* holds on each of *rows*, without loading anything."""
    found: list[Any] = []
    seen: set[int] = set()
    for row in rows:
        value = sa_inspect(row).dict.get(name)
        items = value if isinstance(value, (list, set, tuple)) else [value]
        for item in items:
            if item is not None and id(item) not in seen:
                seen.add(id(item))
                found.append(item)
    return found


def _held(row: Any, name: str) -> Any:
    """What *name* holds on *row*: the collection, the related row or ``None``."""
    return sa_inspect(row).dict.get(name)


def _keep_only(rows: list[Any], name: str, load: _PlannedLoad, kept: set[int]) -> None:
    """Leave in *name* on each of *rows* only the related rows in *kept*.

    A relation with changes the session has not flushed is the app's to keep;
    its resolver reads it again under the scope instead. One marked as loaded
    with its scope stays marked, for what it holds now.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    from strawberry_orm.lazy_resolution import (
        mark_relations_scoped,
        relation_cache_is_scoped,
    )

    for row in rows:
        state = sa_inspect(row)
        held = state.dict.get(name)
        if held is None or state.attrs[name].history.has_changes():
            continue
        items = list(held) if isinstance(held, (list, set, tuple)) else [held]
        if all(id(item) in kept for item in items):
            continue
        marked = relation_cache_is_scoped(row, name, scope=load.token, held=_held)
        remaining = [item for item in items if id(item) in kept]
        set_committed_value(
            row, name, remaining if load.uselist else next(iter(remaining), None)
        )
        if marked:
            mark_relations_scoped([row], [name], scope=load.token, held=_held)


def _holds(rows: list[Any], name: str, load: _PlannedLoad) -> bool:
    """Whether every row has *name* loaded, by a scoped load if it needs one."""
    from strawberry_orm.lazy_resolution import relation_cache_is_scoped

    return all(
        name not in sa_inspect(row).unloaded
        and (
            not load.scoped
            or relation_cache_is_scoped(row, name, scope=load.token, held=_held)
        )
        for row in rows
    )


#: In a session's ``info``: ``[mark, turn]``, the execution that last used it
#: and a token that changes whenever another one does.
_SESSION_TURN = "strawberry_orm_turn"


def _note_statement(state: Any) -> None:
    from strawberry_orm.lazy_resolution import execution_mark

    turn = state.session.info.get(_SESSION_TURN)
    mark = execution_mark()
    if turn is not None and turn[0] is not mark:
        turn[:] = [mark, object()]


@dataclass(frozen=True)
class _Window:
    """A load's turn on its session, and the relations unloaded as it began.

    Executions sharing a session share its rows, and one execution's loads
    fill the rows another is loading. A load marks the relations it filled
    only if no other execution ran a statement on the session meanwhile.
    """

    turn: list[Any]
    token: object
    unloaded: dict[int, set[str]]

    def kept(self) -> bool:
        return self.turn[1] is self.token


def _unloaded_levels(
    model: type, rows: list[Any], level: dict[str, _PlannedLoad]
) -> list[tuple[type, list[Any], dict[str, _PlannedLoad]]]:
    """Cut *level* down to what *rows* do not hold: ``(model, rows, level)``.

    A relation every row holds is dropped, and what is under it is asked of
    the rows it holds, so only a missing level is read. Parents come first.
    """
    missing: dict[str, _PlannedLoad] = {}
    below: list[tuple[type, list[Any], dict[str, _PlannedLoad]]] = []
    for name, load in level.items():
        if not _holds(rows, name, load):
            missing[name] = load
            continue
        children = _related_rows(rows, name)
        if children and load.children:
            below.extend(_unloaded_levels(load.mapper.class_, children, load.children))
    if missing:
        return [(model, rows, missing), *below]
    return below


def _adopt(rows: list[Any], read: list[Any], pk_name: str, names: Any) -> None:
    """Give rows in no session the relations a re-read loaded onto copies.

    A row in the session that re-read it is the very object read, so only a
    detached row differs from what came back.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    by_pk = {getattr(copy, pk_name): copy for copy in read}
    for row in rows:
        copy = by_pk.get(getattr(row, pk_name))
        if copy is None or copy is row:
            continue
        loaded = sa_inspect(copy).dict
        for name in names:
            value = loaded.get(name)
            set_committed_value(
                row, name, list(value) if isinstance(value, list) else value
            )


def _drive(steps: Steps) -> Any:
    """Run *steps* to the end, awaiting only once a step is awaitable."""
    from inspect import isawaitable

    value = None
    try:
        while True:
            effect = steps.send(value)
            if isawaitable(effect):
                return _drive_async(steps, effect)
            value = effect
    except StopIteration as stop:
        return stop.value


async def _drive_async(steps: Steps, effect: Any) -> Any:
    from strawberry_orm._async import await_maybe

    value = await effect
    try:
        while True:
            value = await await_maybe(steps.send(value))
    except StopIteration as stop:
        return stop.value


def _primary_key(value: Any) -> Any:
    return getattr(value, "id", getattr(value, "pk", None))


def _invoke_aggregate_handler(
    handler: Callable[..., Any],
    columns: Any,
    *,
    info: Any = None,
) -> Any:
    """Call a ``@aggregate_field`` handler with ``(self, columns)``."""
    import inspect as _inspect

    sig = _inspect.signature(handler)
    kwargs: dict[str, Any] = {}
    if "info" in sig.parameters:
        kwargs["info"] = info
    return handler(None, columns, **kwargs)


class SQLAlchemyBackend(BaseBackend):
    """Backend adapter for SQLAlchemy."""

    def __init__(
        self,
        *,
        dialect: str = "postgresql",
        session_getter: Callable[..., Any] | None = None,
        filter_overrides: dict[type, type] | None = None,
        max_filter_depth: int = 10,
        max_filter_branches: int = 50,
        enable_regex_filters: bool = False,
        max_in_list_size: int = 500,
        offload_sync_session: bool = False,
        field_order: Callable[[type, list[str]], Iterable[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs["filter_overrides"] = filter_overrides or {}
        super().__init__(**kwargs)
        self._dialect = dialect
        self._session_getter = session_getter
        self._offload_sync_session = offload_sync_session
        self._field_order = field_order
        self._max_filter_depth = max_filter_depth
        self._max_filter_branches = max_filter_branches
        self._enable_regex_filters = enable_regex_filters
        self._max_in_list_size = max_in_list_size

    # -- Introspection -------------------------------------------------------

    def _introspect_model(
        self, model: type
    ) -> list[tuple[str, type, bool, type | None]]:
        """Return (field_name, python_type, is_relation, related_model) for each
        column and relationship on an SQLAlchemy mapped class.

        Columns come first, then relations, unless ``field_order=`` reorders
        them; any name it leaves out follows the ones it lists.
        """
        fields = _introspect_sa_model(model)
        if self._field_order is None:
            return fields
        by_name = {field[0]: field for field in fields}
        ordered = [
            by_name.pop(name)
            for name in self._field_order(model, list(by_name))
            if name in by_name
        ]
        return ordered + list(by_name.values())

    def _get_pk_names(self, model: type) -> set[str]:
        return _get_sa_pk_names(model)

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")
        group = kwargs.get("group")
        aggregate = kwargs.get("aggregate")

        def decorator(cls: type) -> Any:
            if "is_type_of" not in cls.__dict__:
                cls.is_type_of = model_is_type_of(model)  # type: ignore[method-assign]

            from sqlalchemy import inspect as sa_inspect

            mapper = sa_inspect(model)

            col_types: dict[str, type] = {}
            for prop in mapper.column_attrs:
                col = prop.columns[0]
                py_type = _sa_python_type(col.type)
                if col.nullable:
                    py_type = py_type | None
                col_types[prop.key] = py_type

            rel_names = {rel.key for rel in mapper.relationships}

            # PEP 563: resolve string annotations on get_type_hints
            annotations = getattr(cls, "__annotations__", {}).copy()
            try:
                import typing as _t

                resolved = _t.get_type_hints(
                    cls,
                    localns={
                        "auto": strawberry.auto,
                        **{k: v for k, v in vars(cls).items()},
                    },
                    include_extras=True,
                )
                for k in list(annotations):
                    if k in resolved:
                        annotations[k] = resolved[k]
            except Exception:
                pass
            cls.__annotations__ = annotations

            type_name = self._process_type_annotations(
                cls,
                model,
                col_types,
                include=include,
                exclude=exclude,
                name=name,
                filters=filters,
                order=order,
                group=group,
                aggregate=aggregate,
            )

            # SA-specific: relation resolvers for list fields
            for field_name, ann in cls.__annotations__.items():
                if field_name in vars(cls):
                    continue
                if field_name not in rel_names:
                    continue
                rel = mapper.relationships[field_name]
                rel_model = rel.mapper.class_
                el_type = extract_element_type(ann)
                if el_type is None:
                    # A to-one relation. Nothing to generate arguments from,
                    # but it still needs a resolver so the scope is applied.
                    setattr(
                        cls,
                        field_name,
                        _make_sa_plain_rel_resolver(
                            self, field_name, model, rel_model, ann, uselist=False
                        ),
                    )
                    continue
                f_type = getattr(el_type, "__orm_filter__", None)
                o_type = getattr(el_type, "__orm_order__", None)
                if f_type is None and o_type is None:
                    # No arguments to generate, but the relation still needs a
                    # resolver: the default attribute read would emit an
                    # unscoped lazy SELECT whenever the parents arrive already
                    # materialized.
                    setattr(
                        cls,
                        field_name,
                        _make_sa_plain_rel_resolver(
                            self, field_name, model, rel_model, ann
                        ),
                    )
                    continue
                setattr(
                    cls,
                    field_name,
                    _make_sa_rel_resolver(
                        self,
                        field_name,
                        model,
                        rel_model,
                        f_type,
                        o_type,
                    ),
                )

            self._check_lazy_relation_fields(cls, model, cls.__annotations__)
            return self._finalize_type(cls, model, type_name, name)

        return decorator

    def input(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        exclude_pk = kwargs.get("exclude_pk", True)
        name = kwargs.get("name")

        pk_names = _get_sa_pk_names(model) if exclude_pk else set()

        fields_meta = self._introspect_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        for fname, ftype, is_relation, _rel_model in fields_meta:
            if fname in pk_names:
                continue
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if (
                self._exclude_sensitive_fields
                and not (include and fname in include)
                and _SENSITIVE_PATTERNS.search(fname)
            ):
                continue
            if is_relation:
                continue
            annotations[fname] = ftype | None
            defaults[fname] = strawberry.UNSET

        type_name = name or f"{model.__name__}Input"
        self._check_input_does_not_expose_excluded(model, type_name, annotations)
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        self._input_registry.setdefault(model, []).append((type_name, set(annotations)))
        return strawberry.input(cls)

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Callable[..., bool] | None = None,
    ) -> Any:
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._apply_ref_list_async(
                session,
                instance,
                field,
                refs,
                info,
                authorize=authorize,
            )

        return self._apply_ref_list_sync(
            session,
            instance,
            field,
            refs,
            info,
            authorize,
        )

    # -- Upserts -------------------------------------------------------------

    def apply_upsert(
        self, payload: Any, info: Any, *, authorize: Callable[..., bool] | None = None
    ) -> Any:
        from strawberry_orm.upserts import run_sync

        session = self._get_session(info)
        writer = self._upsert_writer(info, authorize)
        if self._is_async_session(session):
            return self._apply_upsert_async(session, writer, payload)
        writer.validate_root(payload)
        with _database_errors(WRITE_REFUSED), session.begin_nested():
            return run_sync(writer.apply_root(payload))

    async def _apply_upsert_async(self, session: Any, writer: Any, payload: Any) -> Any:
        from strawberry_orm.upserts import run_async

        writer.validate_root(payload)
        with _database_errors(WRITE_REFUSED):
            async with session.begin_nested():
                return await run_async(writer.apply_root(payload))

    def _upsert_ops(self, info: Any) -> Any:
        return _SQLAlchemyUpsertOps(self, self._get_session(info), info)

    def _upsert_relation_spec(self, model: type, field: str) -> Any:
        from strawberry_orm.mutations import _sqlalchemy_relation_specs

        return _sqlalchemy_relation_specs(model)[field]

    def _upsert_lookup(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        from sqlalchemy import select

        from strawberry_orm.repo import _get_sa_pk_column

        pk_column = _get_sa_pk_column(model)
        conditions = [
            (pk_column if name == "id" else getattr(model, name)) == value
            for name, value in query.items()
        ]
        stmt = repo.scope_query(select(model).where(*conditions), info)
        return stmt.limit(2)

    def _find_for_upsert(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._find_for_upsert_async(repo, model, query, info)
        rows = session.execute(self._upsert_lookup(repo, model, query, info))
        return self._single_upsert_match(model, list(rows.scalars().all()))

    async def _find_for_upsert_async(
        self, repo: Any, model: type, query: dict[str, Any], info: Any
    ) -> Any:
        session = self._get_session(info)
        rows = await session.execute(self._upsert_lookup(repo, model, query, info))
        return self._single_upsert_match(model, list(rows.scalars().all()))

    # -- Query application ----------------------------------------------------

    def apply_filters(
        self, query: Any, filter_input: Any, model: type, info: Any = None
    ) -> Any:
        check_filter_budget(
            filter_input,
            max_depth=self._max_filter_depth,
            max_branches=self._max_filter_branches,
            max_values=self._max_in_list_size,
        )
        with self.filtering(model, info):
            clause, query = _build_sa_filter(
                filter_input,
                model,
                query=query,
                info=info,
                backend=self,
                max_depth=self._max_filter_depth,
                max_branches=self._max_filter_branches,
                enable_regex=self._enable_regex_filters,
                max_in_list_size=self._max_in_list_size,
            )
        if clause is not None:
            query = query.where(clause)
        return query

    def apply_ordering(
        self, query: Any, order_input: Any, model: type, info: Any = None
    ) -> Any:
        from strawberry_orm.grouping import SetOrder, order_terms

        clauses: list[Any] = []
        joins: list[Any] = []
        # The client's order replaces the statement's own, as a Django or
        # Tortoise ``order_by()`` does; SQLAlchemy's appends to it.
        own_order = query._order_by_clauses
        query = query.order_by(None)
        for term in order_terms(self, model, order_input):
            if isinstance(term, SetOrder):
                clause, rows = self._set_order(model, term, info)
                clauses.append(clause)
                query = _carrying(query, rows)
                continue
            entry_clauses, entry_joins, query = _build_sa_ordering(
                term, model, query=query, backend=self
            )
            clauses.extend(entry_clauses)
            joins.extend(entry_joins)
        seen: set[str] = set()
        for join_prop in joins:
            key = str(join_prop)
            if key not in seen:
                seen.add(key)
                query = query.join(join_prop, isouter=True)
        if clauses:
            query = query.order_by(*clauses)
        elif not query._order_by_clauses:
            query = query.order_by(*own_order)
        return query

    # -- Grouping / aggregation -----------------------------------------------

    def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: AggregateMeta
    ) -> Any:
        from sqlalchemy import func, select

        requested = requested_aggregates(info, "aggregates", aggregate_meta) or {}
        if not requested and not aggregate_meta.custom_fields:
            return aggregate_meta.aggregates_type(count=0)

        subq = _unordered(query).subquery()
        agg_cols: list[Any] = []

        if requested.get("count"):
            agg_cols.append(func.count().label("_count"))
        for func_name in _AGGREGATES:
            for fname in requested.get(func_name, []):
                col = subq.c.get(fname)
                if col is not None:
                    agg_cols.append(
                        _sa_aggregate(func_name, col, self._dialect).label(
                            f"_{func_name}_{fname}"
                        )
                    )

        for field_name, handler, _rtype in aggregate_meta.custom_fields:
            expr = _invoke_aggregate_handler(handler, subq.c, info=info)
            if expr is not None:
                agg_cols.append(expr.label(f"_custom_{field_name}"))

        if not agg_cols:
            return aggregate_meta.build_aggregates(None, requested)

        stmt = _carrying(select(*agg_cols).select_from(subq), query)
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._apply_aggregation_async(
                session, stmt, aggregate_meta, requested
            )

        def aggregate() -> Any:
            return aggregate_meta.build_aggregates(
                session.execute(stmt).one(), requested
            )

        return self._run_sync(aggregate)

    async def _apply_aggregation_async(
        self, session: Any, stmt: Any, meta: AggregateMeta, requested: dict
    ) -> Any:
        with _database_errors():
            result = await session.execute(stmt)
            row = result.one()
        return meta.build_aggregates(row, requested)

    def relation_hop(self, model: type, relation: str) -> Any:
        from strawberry_orm.grouping import GroupHop

        prop = sa_inspect(model).relationships[relation]
        return GroupHop(
            relation=relation,
            model=model,
            target=prop.mapper.class_,
            many=bool(prop.uselist),
        )

    def apply_grouping(
        self,
        query: Any,
        paths: list[Any],
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> Any:
        from strawberry_orm.grouping import build_group, within_max_groups

        model = aggregate_meta.model
        requested = (
            requested_aggregates(info, "groups.aggregates", aggregate_meta) or {}
        )
        stmt, values = self._grouped_select(query, model, paths, info)
        aggregates = _build_sa_agg_cols(
            model,
            requested,
            custom_fields=aggregate_meta.custom_fields,
            info=info,
            dialect=self._dialect,
        )
        clauses, joins, carried = _sa_connection_ordering(
            query, order_input, model, backend=self, info=info
        )
        stmt = _carrying(stmt, *carried)
        for join in joins:
            stmt = stmt.outerjoin(join)
        firsts, ordering = _sa_group_ordering(clauses, self._dialect)
        keys = list(values.values())
        stmt = (
            stmt.add_columns(*aggregates, *firsts)
            .group_by(*keys)
            .order_by(*ordering, *keys)
            .limit(self._max_groups + 1)
        )
        return self._execute_then(
            info,
            stmt,
            lambda result: [
                build_group(
                    aggregate_meta,
                    paths,
                    row._mapping,
                    requested,
                    max_key_rows=self._max_group_key_rows,
                )
                for row in within_max_groups(result.all(), self._max_groups)
            ],
        )

    def batch_group_items(
        self,
        query: Any,
        paths: list[Any],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
        per_group_offset: int = 0,
    ) -> Any:
        from collections import defaultdict

        from sqlalchemy import func, select
        from sqlalchemy.orm import aliased

        from strawberry_orm.grouping import key_tuple

        stmt, values = self._grouped_select(query, model, paths, info)
        clauses, joins, carried = _sa_connection_ordering(
            query, order_input, model, backend=self, info=info
        )
        stmt = _carrying(stmt, *carried)
        for join in joins:
            stmt = stmt.outerjoin(join)
        clauses += _missing_pk(clauses, model)
        rn = func.row_number().over(
            partition_by=list(values.values()), order_by=clauses
        )
        ranked = stmt.add_columns(*model.__table__.c, rn.label("_rn")).subquery()
        row = aliased(model, ranked)
        page = _carrying(
            select(row, *(ranked.c[alias] for alias in values))
            .where(
                ranked.c._rn > per_group_offset,
                ranked.c._rn <= per_group_offset + per_group_limit,
            )
            # Ordered by the row number so each group comes back in the order
            # the window put it in; without it the rows arrive however the
            # database found them and the requested order is lost.
            .order_by(ranked.c._rn),
            stmt,
        )

        def collect(result: Any) -> dict[tuple, list[Any]]:
            items_by_key: dict[tuple, list[Any]] = defaultdict(list)
            for found in result.all():
                key = key_tuple(paths, found._mapping)
                items_by_key[key].append(found[0])
            return dict(items_by_key)

        def steps() -> Steps:
            items_by_key = yield self._execute_then(info, page, collect)
            rows = [row for items in items_by_key.values() for row in items]
            kept = yield from self._scope_apart(rows, model, {}, info)
            if len(kept) < len(rows):
                ids = {id(row) for row in kept}
                items_by_key = {
                    key: [row for row in items if id(row) in ids]
                    for key, items in items_by_key.items()
                }
            return items_by_key

        return _drive(steps())

    def group_memberships(
        self, query: Any, paths: list[Any], pks: list[Any], info: Any
    ) -> Any:
        from collections import defaultdict

        from strawberry_orm.grouping import key_tuple

        model = self.query_model(query)
        pk = _get_sa_pk_column(model)
        stmt, _values = self._grouped_select(query, model, paths, info)
        stmt = stmt.add_columns(pk.label("_pk")).where(pk.in_(pks))

        def collect(result: Any) -> dict[Any, set[tuple]]:
            found: dict[Any, set[tuple]] = defaultdict(set)
            for row in result.all():
                found[row._mapping["_pk"]].add(key_tuple(paths, row._mapping))
            return dict(found)

        return self._execute_then(info, stmt, collect)

    def _execute_then(self, info: Any, stmt: Any, finish: Any) -> Any:
        """Run *stmt* on the request's session and hand the result to *finish*."""
        session = self._get_session(info)
        if self._is_async_session(session):

            async def run() -> Any:
                with _database_errors():
                    return finish(await session.execute(stmt))

            return run()
        return self._run_sync(lambda: finish(session.execute(stmt)))

    def _grouped_select(
        self, query: Any, model: type, paths: list[Any], info: Any
    ) -> tuple[Any, dict[str, Any]]:
        """A select over *query*'s rows, once per key, with every path's value.

        Returns the select, whose only columns are the path values labelled by
        their aliases, and those values by alias. Each hop is an outer join to
        the related type's scoped rows, so a hidden related row reads as
        ``None``, as it does on the type. Across a to-many hop the join is to
        the visible rows alone, leaving one ``None`` row for a parent with none.
        """
        from sqlalchemy import and_, select

        keys = _get_sa_pk_columns(model)
        own = query.with_only_columns(*keys).order_by(None)
        stmt = _carrying(
            select().select_from(model).where(_whole_key_in(keys, own)), query
        )
        joined: dict[tuple[str, ...], Any] = {}
        values: dict[str, Any] = {}
        for path in paths:
            entity: Any = model
            chain: tuple[str, ...] = ()
            for hop in path.hops:
                chain = (*chain, hop.relation)
                if chain not in joined:
                    joined[chain], stmt = self._join_hop(stmt, entity, hop, info)
                entity = joined[chain]
            if path.through is not None:
                expr, rows = self._together(
                    model, path, info, limit=self._max_group_key_rows + 1
                )
                stmt = _carrying(stmt, rows)
            elif path.custom is not None:
                expr = invoke_custom_callback(
                    path.custom, path.owner, query=query, value=path.value, info=info
                )
                if isinstance(expr, tuple):
                    expr = expr[0]
            else:
                expr = getattr(entity, path.column)
                if path.interval is not None:
                    expr = _truncate(expr, path.interval, self._dialect)
            values[path.alias] = expr.label(path.alias)
        if any(hop.many for path in paths for hop in path.hops):
            # A row joined to two related rows with the same values would be
            # grouped twice under one key, so its keys are taken once each.
            labelled = {f"_orm_pk{i}": key for i, key in enumerate(keys)}
            pairs = (
                stmt.add_columns(
                    *(key.label(label) for label, key in labelled.items()),
                    *values.values(),
                )
                .distinct()
                .subquery()
            )
            same_row = and_(*(pairs.c[label] == key for label, key in labelled.items()))
            stmt = _carrying(select().select_from(model).join(pairs, same_row), stmt)
            values = {alias: pairs.c[alias] for alias in values}
        return stmt.add_columns(*values.values()), values

    def _together(
        self, model: type, path: Any, info: Any, *, limit: int | None = None
    ) -> tuple[Any, Any]:
        """The related rows *path* reads of each row of *model*, as JSON text.

        The rows the caller may see, their columns in an array each, distinct
        and sorted so a set of rows reads as the same text wherever it is
        read; ``[]`` for none. PostgreSQL sorts again inside the aggregate,
        which alone it promises to keep; SQLite keeps an ordered subquery's.
        At most *limit* of them are read: a group key is refused past
        ``max_group_key_rows`` rows, and reading one more tells it so.

        Comes with the statement for those rows, which the statement the text
        goes in has to carry the row criteria of.
        """
        from sqlalchemy import Text, and_, cast, func, literal_column, select
        from sqlalchemy.dialects.postgresql import aggregate_order_by
        from sqlalchemy.orm import aliased

        hop = path.through
        restrict = self.relation_scope(hop.model, hop.relation, info)
        rows = select(hop.target)
        if restrict is not None:
            rows = restrict(rows, info)
        visible = aliased(hop.target, rows.subquery())
        target = sa_inspect(hop.target)
        columns = [
            (
                getattr(visible, column)
                if interval is None
                else _truncate(getattr(visible, column), interval, self._dialect)
            ).label(f"_orm_c{index}")
            for index, (column, interval) in enumerate(
                zip(path.columns, path.intervals, strict=True)
            )
        ]

        def on_visible(column: Any) -> Any:
            return getattr(visible, target.get_property_by_column(column).key)

        prop = sa_inspect(hop.model).relationships[hop.relation]
        if prop.secondary is None:
            linked = select(*columns).where(
                *(
                    on_visible(remote) == local
                    for local, remote in prop.local_remote_pairs
                )
            )
        else:
            link = prop.secondary.alias()
            linked = (
                select(*columns)
                .select_from(visible)
                .join(
                    link,
                    and_(
                        *(
                            on_visible(column) == link.c[secondary.key]
                            for column, secondary in prop.secondary_synchronize_pairs
                        )
                    ),
                )
                .where(
                    *(
                        link.c[secondary.key] == parent
                        for parent, secondary in prop.synchronize_pairs
                    )
                )
            )
        found = linked.distinct().order_by(*columns).limit(limit)
        found = found.correlate(model).subquery("_orm_t")
        if self._dialect == "sqlite":
            together = func.json_group_array(func.json_array(*found.c))
        elif self._dialect == "postgresql":
            together = cast(
                func.json_agg(
                    aggregate_order_by(func.json_build_array(*found.c), *found.c)
                ),
                Text,
            )
        else:
            raise ValueError(
                f"Grouping or ordering by objects: is supported on PostgreSQL "
                f"and SQLite, not {self._dialect}."
            )
        together = func.coalesce(
            select(together).select_from(found).scalar_subquery(),
            literal_column("'[]'"),
        )
        return together, rows

    def _set_order(self, model: type, by_set: Any, info: Any) -> tuple[Any, Any]:
        """The clause a ``SetOrder`` orders by: the text its set groups under.

        Comes with the statement whose row criteria the ordered one carries.
        """
        together, rows = self._together(model, by_set.path, info)
        return (together.desc() if by_set.descending else together.asc()), rows

    def _join_hop(self, stmt: Any, entity: Any, hop: Any, info: Any) -> Any:
        """Outer-join *hop*'s related rows the caller may see onto *stmt*."""
        from sqlalchemy import and_, select
        from sqlalchemy.orm import aliased

        restrict = self.relation_scope(hop.model, hop.relation, info)
        rows = select(hop.target)
        if restrict is not None:
            rows = restrict(rows, info)
        prop = sa_inspect(hop.model).relationships[hop.relation]
        stmt = _carrying(stmt, rows)
        if prop.secondary is None:
            target = aliased(hop.target, rows.subquery())
            return target, stmt.outerjoin(
                target, getattr(entity, hop.relation).of_type(target)
            )
        # The association table goes inside the scoped rows: joined outside,
        # a link to a hidden row would still add a row of its own.
        link = prop.secondary.alias()
        parents = [
            link.c[secondary.key].label(f"_orm_parent_{index}")
            for index, (_parent, secondary) in enumerate(prop.synchronize_pairs)
        ]
        linked = (
            rows.add_columns(*parents)
            .join(
                link,
                and_(
                    *(
                        column == link.c[secondary.key]
                        for column, secondary in prop.secondary_synchronize_pairs
                    )
                ),
            )
            .subquery()
        )
        target = aliased(hop.target, linked)
        return target, stmt.outerjoin(
            target,
            and_(
                *(
                    linked.c[f"_orm_parent_{index}"] == parent
                    for index, (parent, _secondary) in enumerate(prop.synchronize_pairs)
                )
            ),
        )

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type, *, limited: bool = True) -> Any:
        from sqlalchemy import select

        stmt = select(model)
        if limited and self._default_query_limit is not None:
            stmt = stmt.limit(self._default_query_limit)
        return stmt

    def is_query_object(self, value: Any) -> bool:
        return isinstance(value, Select)

    def is_model_instance(self, value: Any) -> bool:
        # Where default instrumentation keeps an instance's state; a mapped
        # class has no such key. Custom instrumentation can keep it elsewhere,
        # and a row taken for something else would skip its scope check.
        if "_sa_instance_state" in getattr(value, "__dict__", ()):
            return True
        if isinstance(value, type):
            return False
        return isinstance(sa_inspect(value, raiseerr=False), InstanceState)

    def relation_names(self, model: type) -> set[str]:
        # Called once per field per row while resolving, so the set is cached
        # per model class. Relationships are fixed once mappers configure.
        names = _RELATION_NAMES.get(model)
        if names is None:
            names = set(sa_inspect(model).relationships.keys())
            _RELATION_NAMES[model] = names
        return names

    def _relation_target_model(self, model: type, relation: str) -> type | None:
        from sqlalchemy import inspect as sa_inspect

        try:
            rel = sa_inspect(model).relationships.get(relation)
        except Exception:  # pragma: no cover - defensive
            return None
        return rel.mapper.class_ if rel is not None else None

    def instance_pk(self, instance: Any) -> Any:
        from sqlalchemy import inspect as sa_inspect

        try:
            identity = sa_inspect(instance).identity
        except Exception:  # pragma: no cover - defensive
            return None
        if not identity or len(identity) != 1:
            return None
        return identity[0]

    def query_model(self, query: Any) -> type | None:
        try:
            return query.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError, TypeError):
            return None

    def filter_scoped_pks(self, model: type, pks: list[Any], info: Any) -> Any:
        from inspect import isawaitable

        from sqlalchemy import select

        pk_column = _get_sa_pk_column(model)
        scoped = self.apply_type_scope(
            select(model).where(pk_column.in_(pks)), model, info
        )
        # Only the keys: as a subquery, the loader options a scope may attach
        # to load what a selection reads are not run for a membership check.
        stmt = _carrying(
            select(pk_column).where(
                pk_column.in_(scoped.with_only_columns(pk_column).scalar_subquery())
            ),
            scoped,
        )
        keys = self._execute_stmt(stmt, info)
        if isawaitable(keys):

            async def _await_pks() -> set[Any]:
                return set(await keys)

            return _await_pks()
        return set(keys)

    def split_parent_predicate(
        self, query: Any, parent_pk: Any
    ) -> tuple[str, Any, Any] | None:
        from sqlalchemy.sql import operators
        from sqlalchemy.sql.elements import BinaryExpression

        if query._limit_clause is not None or query._offset_clause is not None:
            return None
        criteria = list(getattr(query, "_where_criteria", ()))
        if not criteria:
            return None

        # The key is read back off each result row, so the predicate must sit on
        # the selected entity's own table; a joined column would group rows by a
        # value that is not the one the join matched.
        try:
            entity = query.column_descriptions[0]["entity"]
            base_table = entity.__table__
        except (AttributeError, IndexError, KeyError):
            return None

        hits = []
        for clause in criteria:
            if not isinstance(clause, BinaryExpression):
                # A BooleanClauseList here means OR / NOT; not rewritable.
                return None
            if clause.operator is not operators.eq:
                continue
            column = clause.left
            bound = getattr(clause.right, "value", None)
            # Require a foreign-key column: a boolean column compared to True
            # would otherwise match a parent whose primary key is 1.
            if not getattr(column, "foreign_keys", None):
                continue
            if getattr(column, "table", None) is not base_table:
                continue
            if bound is not None and bound == parent_pk:
                hits.append((clause, column))

        if len(hits) != 1:
            return None

        hit, column = hits[0]
        remainder = query._clone()
        remainder._where_criteria = tuple(c for c in criteria if c is not hit)
        return column.key, column, remainder

    def query_signature(self, query: Any) -> str | None:
        # Rendering literals inline loses type information, so ``title == 1``
        # and ``title == "1"`` would sign identically even though a strictly
        # typed database treats them differently. Sign the parameterised SQL
        # plus typed bind values instead.
        try:
            compiled = query.compile()
            params = compiled.params
        except Exception:  # pragma: no cover - defensive
            return None
        typed = tuple(
            (key, type(params[key]).__name__, repr(params[key]))
            for key in sorted(params)
        )
        return repr((str(compiled), typed))

    def apply_key_filter(
        self, query: Any, attr_name: str, key_handle: Any, keys: list[Any]
    ) -> Any:
        return query.where(key_handle.in_(list(keys)))

    @contextmanager
    def query_probe(self, info: Any) -> Any:
        """Count ORM statements issued on the request session while running."""
        from sqlalchemy import event

        from strawberry_orm.lazy_resolution import QueryProbe

        probe = QueryProbe()
        try:
            session = self._get_session(info)
        except Exception:
            yield probe
            return

        def _on_execute(state: Any) -> None:
            probe.count += 1

        # AsyncSession rejects listeners; they must go on its sync_session.
        target = getattr(session, "sync_session", session)
        event.listen(target, "do_orm_execute", _on_execute)
        try:
            yield probe
        finally:
            event.remove(target, "do_orm_execute", _on_execute)

    def count_query(self, query: Any, info: Any) -> Any:
        from sqlalchemy import func, select

        subq = _unordered(query).subquery()
        counted: Any = subq
        if _may_repeat_rows(query):
            # Rows are read with .unique(), so a join that repeats an entity
            # yields it once; count the distinct entities to match.
            entity = self.query_model(query)
            keys = [
                subq.corresponding_column(c) for c in sa_inspect(entity).primary_key
            ]
            counted = select(*keys).distinct().subquery()
        stmt = _carrying(select(func.count()).select_from(counted), query)
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._scalar_async(session, stmt)
        return self._run_sync(lambda: session.execute(stmt).scalar_one())

    async def _scalar_async(self, session: Any, stmt: Any) -> Any:
        with _database_errors():
            return (await session.execute(stmt)).scalar_one()

    def apply_page(self, query: Any, offset: int, limit: int) -> Any | None:
        from sqlalchemy.exc import CompileError

        if query._fetch_clause is not None:
            return None
        try:
            prior_offset, prior_limit = query._offset or 0, query._limit
        except CompileError:
            # A limit or offset given as an expression cannot be composed.
            return None
        if _may_repeat_rows(query):
            if prior_offset or prior_limit is not None:
                return None
            return _first_appearances_page(query, offset, limit)
        if prior_limit is not None:
            limit = max(0, min(limit, prior_limit - offset))
        missing = _missing_pk(query._order_by_clauses, self.query_model(query))
        if missing:
            query = query.order_by(*missing)
        return query.offset(prior_offset + offset).limit(limit)

    def materialize_query(self, query: Any, info: Any) -> Any:
        return self._execute_stmt(query, info)

    # -- Optimizer -----------------------------------------------------------

    _supports_relation_batching = True
    _supports_windowed_pages = True

    def _make_on_resolver(
        self, model: type, field_name: str, relation: str, return_ann: Any
    ) -> Any:
        return _make_sa_on_resolver(self, model, field_name, relation, return_ann)

    def _make_relation_query_resolver(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        from sqlalchemy import inspect as sa_inspect

        backend = self
        rel = sa_inspect(model).relationships[relation]
        rel_model = self._relation_target_model(model, relation)
        pairs = list(rel.local_remote_pairs or ())

        def resolver(self: Any, info: Any) -> Any:
            from strawberry_orm.batching import take_page

            page = take_page(self, field_name, info)
            if page is not None:
                return page
            from sqlalchemy import select

            stmt = select(rel_model).where(pairs[0][1] == backend.instance_pk(self))
            restrict = backend.relation_scope(model, field_name, info, on=relation)
            return stmt if restrict is None else restrict(stmt, info)

        resolver.__name__ = field_name
        return resolver

    def group_counts(self, query: Any, key_field: str, info: Any) -> Any:
        from sqlalchemy import func, select

        subq = _unordered(query).subquery()
        key_col = subq.c[key_field]
        stmt = _carrying(
            select(key_col, func.count()).select_from(subq).group_by(key_col), query
        )
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._group_counts_async(session, stmt)
        return self._run_sync(lambda: dict(session.execute(stmt).all()))

    async def _group_counts_async(self, session: Any, stmt: Any) -> dict[Any, int]:
        with _database_errors():
            return dict((await session.execute(stmt)).all())

    def _relation_connection_spec(
        self, model: type, field_name: str, relation: str
    ) -> Any:
        from sqlalchemy import inspect as sa_inspect

        from strawberry_orm.backends._base import RelationConnectionSpec

        rel = sa_inspect(model).relationships[relation]
        pairs = list(rel.local_remote_pairs or ())
        # An association table keeps the parent key off the related row, so
        # there is nothing for a window to partition by.
        if rel.secondary is not None or len(pairs) != 1:
            return None
        return RelationConnectionSpec(
            model=model,
            field_name=field_name,
            relation=relation,
            related_model=self._relation_target_model(model, relation),
            key_field=pairs[0][1].key,
        )

    def relation_base_query(self, spec: Any, pks: list[Any], info: Any) -> Any:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import select

        rel = sa_inspect(spec.model).relationships[spec.relation]
        fk_column = list(rel.local_remote_pairs)[0][1]
        stmt = select(spec.related_model).where(fk_column.in_(pks))
        restrict = self.relation_scope(
            spec.model, spec.field_name, info, on=spec.relation
        )
        return stmt if restrict is None else restrict(stmt, info)

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def _apply_nested_queryset(
        self,
        stmt: Any,
        parent_entity: type,
        field_name: str,
        related_entity: type,
        info: Any,
    ) -> Any:
        """Re-apply child scoping for nested relation resolvers."""
        get_qs = self._type_querysets.get(related_entity)
        if get_qs is not None:
            stmt = get_qs(stmt, info)

        type_name = self._resolved_type_name(parent_entity, info)
        if type_name:
            hints = self._store.get(type_name, field_name)
            if hints and hints.scope is not None:
                stmt = call_scope(hints.scope, stmt, info)

        return stmt

    def _plan_relation_loads(
        self, store: Any, entity: type, info: Any
    ) -> tuple[dict[str, _PlannedLoad], list[tuple[type, str, Any]]]:
        """Plan the relations the current selection reads below *entity*.

        Shared by query optimization and by loading relations onto instances
        the caller already holds, so both apply the same row scoping. Returns
        the plan and each relation it loads, with the ``scope=`` its load
        applies, or ``_UNMARKED`` when the load does not apply the scope.
        """
        from sqlalchemy import inspect as sa_inspect

        from strawberry_orm.optimizer.selections import (
            field_nodes_from_info,
            fragments_from_info,
            iter_field_nodes,
        )

        fragments = fragments_from_info(info)
        mapper = sa_inspect(entity)
        scoped_edges: list[tuple[type, str, Any]] = []

        def _get_field_name(node: Any) -> str:
            """Extract field name from a GraphQL FieldNode, converting camelCase
            to snake_case to match ORM attribute names."""
            name = node.name.value
            import re

            return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

        def _scope_load(
            load: _PlannedLoad, owner: str, type_name: str | None, field_name: str
        ) -> None:
            """Scope *load* by the related type's ``scope_rows`` and the edge's ``scope=``.

            A loader option carries only a WHERE clause, so it carries the
            scope (``criteria``) only when that is all the scope says. One
            that says more is read whole, by a statement of its own narrowed
            to the parents' keys, and a relation with no such keys refuses it.
            """
            from sqlalchemy import select as sa_select

            related_entity = load.mapper.class_
            get_qs = self._type_querysets.get(related_entity)
            hints = store.get(type_name, field_name) if type_name and store else None
            field_scope = hints.scope if hints else None
            if get_qs is None and field_scope is None:
                return

            def scope(stmt: Any) -> Any:
                if get_qs is not None:
                    stmt = get_qs(stmt, info)
                if field_scope is not None:
                    stmt = call_scope(field_scope, stmt, info)
                return stmt

            scoped = scope(sa_select(related_entity))
            load.scope = scope
            load.token = field_scope
            if load.keys is not None:
                load.ordered = load.uselist and bool(scoped._order_by_clauses)
            if _says_only_where(scoped, related_entity):
                load.criteria = scoped.whereclause
                return
            if load.keys is None:
                raise ValueError(
                    f"Cannot load {owner}.{field_name} with the row scope of "
                    f"{related_entity.__name__}: the scope does more than a "
                    f".where() on {related_entity.__name__}'s own columns (a "
                    f"join, a nested select, raw SQL or loader options), and "
                    f"this relation does not join on one column, so the scope "
                    f"cannot be applied to it whole. Write the scope as a "
                    f".where() on those columns, or read this relation in a "
                    f"resolver of your own."
                )
            load.whole = True

        # A relation reached both by the selection and by a ``using=`` path
        # must be loaded once, with one set of criteria. Two loaders on one
        # attribute leave it to SQLAlchemy which rows end up there.
        plan: dict[str, _PlannedLoad] = {}

        def _planned(
            level: dict[str, _PlannedLoad],
            current_mapper: Any,
            rel_name: str,
            type_name: str | None,
        ) -> _PlannedLoad:
            load = level.get(rel_name)
            if load is None:
                rel = current_mapper.relationships[rel_name]
                load = level[rel_name] = _PlannedLoad(
                    attr=getattr(current_mapper.entity, rel_name),
                    uselist=rel.uselist,
                    mapper=rel.mapper,
                    criteria=None,
                )
                join_keys = _join_keys(rel)
                if join_keys is not None:
                    local, remote, load.link = join_keys
                    load.keys = (
                        current_mapper.get_property_by_column(local).key,
                        remote,
                        rel.mapper.get_property_by_column(remote).key,
                    )
                _scope_load(
                    load,
                    type_name or current_mapper.class_.__name__,
                    type_name,
                    rel_name,
                )
                scoped_edges.append(
                    (
                        current_mapper.class_,
                        rel_name,
                        load.token
                        if load.criteria is not None or load.reads_alone()
                        else _UNMARKED,
                    )
                )
            return load

        def _plan_hints(
            level: dict[str, _PlannedLoad],
            current_mapper: Any,
            type_name: str | None,
            field_name: str,
        ) -> None:
            """Plan the relation paths declared on ``using=`` for *field_name*.

            Keyed on the field name alone, so computed fields that are not mapped
            attributes can still declare the relations their resolver reads.
            """
            hints = store.get(type_name, field_name) if type_name and store else None
            if not hints or hints.disable_optimization or not hints.using:
                return
            for path in hints.using:
                hop_level, hop_mapper, hop_type = level, current_mapper, type_name
                for hop in hint_hops(path):
                    if hop not in hop_mapper.relationships:
                        break
                    load = _planned(hop_level, hop_mapper, hop, hop_type)
                    hop_type = self._child_type_name(
                        hop_type, hop, load.mapper.class_, info
                    )
                    hop_level, hop_mapper = load.children, load.mapper

        def _plan_selection(
            level: dict[str, _PlannedLoad],
            selection_set: Any,
            current_mapper: Any,
            type_name: str | None,
        ) -> None:
            for node in iter_field_nodes(selection_set, fragments):
                field_name = _get_field_name(node)

                # A field that answers for itself will ignore whatever this
                # loads, so do not pay for it.
                if self.resolves_itself(type_name, field_name):
                    continue
                _plan_hints(level, current_mapper, type_name, field_name)
                if field_name not in current_mapper.relationships:
                    continue
                load = _planned(level, current_mapper, field_name, type_name)
                if node.selection_set:
                    child_type = self._child_type_name(
                        type_name, node.name.value, load.mapper.class_, info
                    )
                    _plan_selection(
                        load.children, node.selection_set, load.mapper, child_type
                    )

        root_type_name = self._selection_type_name(entity, info)
        for field_node in field_nodes_from_info(info):
            if field_node.selection_set is not None:
                _plan_selection(plan, field_node.selection_set, mapper, root_type_name)
        return plan, scoped_edges

    def _keyed_loads(
        self,
        session: Any,
        rows: list[Any],
        level: dict[str, _PlannedLoad],
        *,
        every: bool = False,
    ) -> Steps:
        """Read relations by a statement of their own, for every row at once.

        A loader option orders only by ``relationship(order_by=...)`` and
        carries only a WHERE clause, so a relation whose scope orders or says
        more is read as Django's ``Prefetch`` reads it: the scope's own
        statement, narrowed to the rows' keys, then handed to each row. With
        *every*, so is each to-many relation keyed on the related row, which
        then needs no statement re-reading the rows.

        Only rows whose relation is unloaded are filled: a collection the app
        already holds may carry changes the database has not seen.
        """
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import set_committed_value

        from strawberry_orm.lazy_resolution import mark_relations_scoped

        for name, load in level.items():
            if not rows:
                return
            if not load.reads_alone(every):
                if _reads_alone_below(load.children):
                    yield from self._keyed_loads(
                        session, _related_rows(rows, name), load.children
                    )
                continue
            missing = [row for row in rows if name in sa_inspect(row).unloaded]
            local_key, remote, remote_key = load.keys
            keys = [
                key
                for key in dict.fromkeys(getattr(row, local_key) for row in missing)
                if key is not None
            ]
            links = (
                [(key, key) for key in keys]
                if load.link is None
                else (yield from self._linked_keys(session, load.link, keys))
            )
            parents_of: dict[Any, list[Any]] = {}
            for parent, key in links:
                parents_of.setdefault(key, []).append(parent)
            keys = list(parents_of)
            order_by = load.attr.property.order_by
            options = _loader_options(load.children)
            children: list[Any] = []
            for start in range(0, len(keys), _KEYS_PER_STATEMENT):
                chunk = keys[start : start + _KEYS_PER_STATEMENT]
                stmt = select(load.mapper.class_).where(remote.in_(chunk))
                if load.scope is not None:
                    stmt = load.scope(stmt)
                if not load.ordered and order_by:
                    stmt = stmt.order_by(*order_by)
                if options:
                    stmt = stmt.options(*options)
                children.extend((yield self._execute_in(session, stmt)))
            grouped: dict[Any, list[Any]] = {}
            for child in children:
                for parent in parents_of.get(getattr(child, remote_key), ()):
                    grouped.setdefault(parent, []).append(child)
            for row in missing:
                held = grouped.get(getattr(row, local_key), [])
                set_committed_value(
                    row, name, held if load.uselist else next(iter(held), None)
                )
            if load.scoped:
                mark_relations_scoped(missing, [name], scope=load.token, held=_held)
            yield from self._keyed_loads(session, children, load.children)

    def _linked_keys(
        self, session: Any, link: tuple[Any, Any], keys: list[Any]
    ) -> Steps:
        """``(parent key, related key)`` for each row of an association table."""
        from sqlalchemy import select

        parent_link, related_link = link
        links: list[tuple[Any, Any]] = []
        for start in range(0, len(keys), _KEYS_PER_STATEMENT):
            chunk = keys[start : start + _KEYS_PER_STATEMENT]
            stmt = select(parent_link, related_link).where(parent_link.in_(chunk))
            links.extend((yield self._execute_rows_in(session, stmt)))
        return list(dict.fromkeys(links))

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        try:
            entity = query.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError):
            return query

        # A statement that already eager-loads may keep that unscoped load
        # when another loader for the same relationship is added. Don't mark
        # those caches as trusted; the resolver re-checks them.
        caller_had_options = bool(getattr(query, "_with_options", ()))
        query = self.apply_type_scope(query, entity, info)
        plan, edges = self._plan_relation_loads(store, entity, info)
        options = _loader_options(plan)
        loaded = query.options(*options) if options else query
        session = self._get_session(info)
        trusted = edges if not caller_had_options and _marks_some(edges) else []

        def steps() -> Steps:
            window = (yield self._open_window(session, [])) if trusted else None
            rows = yield self._execute_in(session, loaded)
            yield from self._keyed_loads(session, rows, plan)
            rows = yield from self._scope_apart(rows, entity, plan, info)
            if window is not None and window.kept():
                self._mark_scoped_edges(rows, trusted, before=window.unloaded)
            return rows

        return _drive(steps())

    def _scope_apart(
        self, rows: list[Any], model: type, level: dict[str, _PlannedLoad], info: Any
    ) -> Steps:
        """*rows* read under *model*'s scope, less the subclass rows theirs hides.

        A row of a mapped subclass whose type defines its own ``scope_rows``
        is scoped by that, not by the scope the read ran under. The relations
        *level* loaded onto the rows were read the same way, so each one gives
        up the rows of such a subclass that its scope hides.
        """
        apart = self.rows_scoped_apart(rows, model)
        if apart:
            kept = {id(row) for row in (yield self.scope_instances(apart, info))}
            hidden = {id(row) for row in apart if id(row) not in kept}
            rows = [row for row in rows if id(row) not in hidden]
        for name, load in level.items():
            held = _related_rows(rows, name)
            if not held:
                continue
            kept_held = yield from self._scope_apart(
                held, load.mapper.class_, load.children, info
            )
            if len(kept_held) < len(held):
                _keep_only(rows, name, load, {id(row) for row in kept_held})
        return rows

    def load_relations(self, store: Any, instances: list[Any], info: Any) -> list[Any]:
        """Eager-load the selected relations onto instances already in memory.

        Each row is re-selected in its own session, whose identity map hands
        back the very object, so its unloaded relations fill in place. Scalars
        already loaded are left alone, so values fresher than the database
        survive. A relation every row already holds is not read again, unless
        something under it is missing; then only that is read.

        Returns the rows the selection planned loads for, which is empty when
        it named no relations.
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import select

        planned, groups, edges = self._relation_load_groups(store, instances, info)
        if not planned:
            return []

        sessions = {id(session): session for session, _model, _rows, _level in groups}

        def steps() -> Steps:
            windows: list[_Window] = []
            if _marks_some(edges):
                for session in sessions.values():
                    windows.append((yield self._open_window(session, planned)))
            for session, model, rows, level in groups:
                reread = {
                    name: load
                    for name, load in level.items()
                    if not load.reads_alone(every=True)
                }
                options = _loader_options(reread)
                if options:
                    mapper = sa_inspect(model)
                    pk_column = mapper.primary_key[0]
                    pk_name = mapper.get_property_by_column(pk_column).key
                    stmt = select(model).where(
                        pk_column.in_([getattr(row, pk_name) for row in rows])
                    )
                    copies = yield self._execute_in(session, stmt.options(*options))
                    _adopt(rows, copies, pk_name, reread)
                yield from self._keyed_loads(session, rows, level, every=True)
                # The rows themselves were checked before anything was loaded.
                yield from self._scope_apart(rows, model, level, info)
            if windows and all(window.kept() for window in windows):
                before: dict[int, set[str]] = {}
                for window in windows:
                    before.update(window.unloaded)
                self._mark_scoped_edges(planned, edges, before=before)
            return planned

        return _drive(steps())

    def _relation_load_groups(
        self, store: Any, instances: list[Any], info: Any
    ) -> tuple[list[Any], list[tuple[Any, type, list[Any], Any]], list[Any]]:
        """What each session's rows still need loaded.

        Rows are grouped by the session they belong to; only rows in none fall
        back to the request's. Each group's plan is cut down to the relations
        some row has not loaded, so a group whose rows hold everything already
        costs nothing.
        """
        fallback: list[Any] = []
        by_session: dict[tuple[int, type], tuple[Any, list[Any]]] = {}
        for instance in instances:
            session = self._row_session(instance)
            if session is None:
                if not fallback:
                    fallback.append(self._get_session(info))
                session = fallback[0]
            key = (id(session), type(instance))
            by_session.setdefault(key, (session, []))[1].append(instance)

        planned: list[Any] = []
        groups: list[tuple[Any, type, list[Any], Any]] = []
        edges: list[Any] = []
        for (_session_id, model), (session, rows) in by_session.items():
            plan, scoped = self._plan_relation_loads(store, model, info)
            if not plan:
                continue
            planned.extend(rows)
            edges.extend(scoped)
            for level_model, level_rows, level in _unloaded_levels(model, rows, plan):
                groups.append((session, level_model, level_rows, level))
        return planned, groups, edges

    def _row_session(self, row: Any) -> Any:
        """The session *row* belongs to, as the caller holds it, or ``None``."""
        from sqlalchemy.orm import object_session

        session = object_session(row)
        if session is None:
            return None
        asyncio_module = sys.modules.get("sqlalchemy.ext.asyncio")
        if asyncio_module is None:
            return session
        return asyncio_module.async_object_session(row) or session

    def _open_window(self, session: Any, extra: list[Any]) -> Any:
        """Take a turn on *session* and note what it has unloaded, as one step.

        An offloaded session runs it on the worker thread, after any statement
        already running there has filled its rows.
        """
        if self._is_async_session(session):
            return self._window(session, extra)
        return self._run_sync(self._window, session, extra)

    def _window(self, session: Any, extra: list[Any]) -> _Window:
        from sqlalchemy import event

        from strawberry_orm.lazy_resolution import execution_mark

        sync = getattr(session, "sync_session", session)
        if not event.contains(sync, "do_orm_execute", _note_statement):
            event.listen(sync, "do_orm_execute", _note_statement)
        turn = sync.info.setdefault(_SESSION_TURN, [None, None])
        mark = execution_mark()
        if turn[0] is not mark or turn[1] is None:
            turn[:] = [mark, object()]
        return _Window(turn, turn[1], self._unloaded_snapshot(session, extra))

    def _unloaded_snapshot(self, session: Any, extra: list[Any]) -> dict[int, set[str]]:
        """Relations still unloaded, so a later load can see what it filled.

        Objects already in the identity map keep whatever they had loaded.
        Marking those caches would trust a relation this query did not refresh.
        """
        from sqlalchemy import inspect as sa_inspect

        snap: dict[int, set[str]] = {}

        def add(obj: Any) -> None:
            if not self.is_model_instance(obj) or id(obj) in snap:
                return
            snap[id(obj)] = set(sa_inspect(obj).unloaded)

        for obj in extra:
            add(obj)
        sync = getattr(session, "sync_session", session)
        identity_map = getattr(sync, "identity_map", None)
        if identity_map is None:
            return snap
        for obj in identity_map.values():
            add(obj)
        return snap

    def _mark_scoped_edges(
        self,
        roots: Any,
        edges: list[tuple[type, str, Any]],
        *,
        before: dict[int, set[str]],
    ) -> None:
        """Mark relations a scoped loader populated during this query.

        An object missing from *before* was created by the load, so every
        relation it now holds came from that loader. An object that was
        already persistent is marked only for relations that moved from
        unloaded to loaded.

        Each edge comes with the scope it was loaded with. One relation planned
        in two places under different scopes cannot tell which load filled an
        object, so it is not marked at all.
        """
        from collections import defaultdict

        from sqlalchemy import inspect as sa_inspect

        from strawberry_orm.lazy_resolution import mark_relations_scoped

        by_model: dict[type, dict[str, Any]] = defaultdict(dict)
        for model, name, token in edges:
            names = by_model[model]
            names[name] = token if names.get(name, token) is token else _UNMARKED
        seen: set[int] = set()

        def walk(obj: Any) -> None:
            if obj is None or id(obj) in seen or not self.is_model_instance(obj):
                return
            seen.add(id(obj))
            state = sa_inspect(obj)
            # Computed afresh on every read, so read once per object.
            unloaded = state.unloaded
            was = before.get(id(obj))
            for name, token in by_model.get(type(obj), {}).items():
                if (
                    token is not _UNMARKED
                    and name not in unloaded
                    and (was is None or name in was)
                ):
                    mark_relations_scoped([obj], [name], scope=token, held=_held)
            # Iterating this namespace yields relationship objects, not names.
            for name in state.mapper.relationships.keys():  # noqa: SIM118
                if name in unloaded:
                    continue
                value = state.dict.get(name)
                if isinstance(value, list):
                    for child in value:
                        walk(child)
                else:
                    walk(value)

        for root in roots:
            walk(root)

    # -- Helpers -------------------------------------------------------------

    def _get_session(self, info: Any) -> Any:
        if self._session_getter is not None:
            return self._session_getter(info)
        ctx = info.context
        if isinstance(ctx, dict):
            if "session" in ctx:
                s = ctx["session"]
                if callable(s):
                    raise TypeError(
                        "info.context['session'] is callable. "
                        "Use session_getter= on the backend instead."
                    )
                return s
        else:
            if hasattr(ctx, "session"):
                s = ctx.session
                if callable(s):
                    raise TypeError(
                        "info.context.session is callable. "
                        "Use session_getter= on the backend instead."
                    )
                return s
            if hasattr(ctx, "get_session"):
                return ctx.get_session()
        raise RuntimeError(
            "SQLAlchemy backend requires a session_getter or info.context.session"
        )

    def _is_async_session(self, session: Any) -> bool:
        # Importing sqlalchemy.ext.asyncio needs greenlet, which a sync-only
        # app may not have; an AsyncSession exists only once it is imported.
        asyncio_module = sys.modules.get("sqlalchemy.ext.asyncio")
        if asyncio_module is None:
            return False
        return isinstance(session, asyncio_module.AsyncSession)

    def _execute_stmt(self, stmt: Any, info: Any) -> Any:
        return self._execute_in(self._get_session(info), stmt)

    def _execute_in(self, session: Any, stmt: Any) -> Any:
        if self._is_async_session(session):
            return self._execute_stmt_async(session, stmt)
        return self._run_sync(self._execute_stmt_sync, session, stmt)

    def _execute_rows_in(self, session: Any, stmt: Any) -> Any:
        """The rows of a statement over plain columns, as tuples."""

        def read(sync_session: Any) -> list[tuple[Any, ...]]:
            with _database_errors():
                return [tuple(row) for row in sync_session.execute(stmt)]

        if self._is_async_session(session):
            return session.run_sync(read)
        return self._run_sync(read, session)

    def _run_sync(self, work: Callable[..., Any], *args: Any) -> Any:
        """Run *work* on a sync session, off the event loop if asked to.

        Offloaded, every statement runs on one worker thread
        (``thread_sensitive``) and an awaitable comes back, as from an
        ``AsyncSession``. It is opt-in: a pool that keeps a connection per
        thread, as in-memory SQLite's does, would find another database there.
        """
        if self._offload_sync_session:
            return run_orm_work(_sanitizing, work, *args)
        return _sanitizing(work, *args)

    def _execute_stmt_sync(self, session: Any, stmt: Any) -> list[Any]:
        with _database_errors():
            return list(session.execute(stmt).scalars().unique().all())

    async def _execute_stmt_async(self, session: Any, stmt: Any) -> list[Any]:
        with _database_errors():
            result = await session.execute(stmt)
            return list(result.scalars().unique().all())

    def _apply_ref_list_sync(
        self,
        session: Any,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        authorize: Callable[..., bool] | None,
    ) -> None:
        from strawberry_orm.upserts import run_sync

        writer = self._ref_list_writer(instance, field, refs, info, authorize)
        spec = self._upsert_relation_spec(type(instance), field)
        with _database_errors(WRITE_REFUSED), session.begin_nested():
            run_sync(writer.apply_refs(instance, spec, refs))

    async def _apply_ref_list_async(
        self,
        session: Any,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Callable[..., bool] | None,
    ) -> None:
        from strawberry_orm.upserts import run_async

        writer = self._ref_list_writer(instance, field, refs, info, authorize)
        spec = self._upsert_relation_spec(type(instance), field)
        with _database_errors(WRITE_REFUSED):
            async with session.begin_nested():
                await run_async(writer.apply_refs(instance, spec, refs))


class _SQLAlchemyUpsertOps:
    """The calls :class:`~strawberry_orm.upserts.UpsertWriter` makes.

    On an async session, a call that may need I/O returns a coroutine for the
    async driver to await; everything else answers directly.
    """

    def __init__(self, backend: SQLAlchemyBackend, session: Any, info: Any) -> None:
        self._session = session
        self._info = info
        self._async = backend._is_async_session(session)

    # -- Repo-backed reads and writes ----------------------------------------

    def find(self, repo: Any, model: type, query: dict[str, Any]) -> Any:
        if self._async:
            return repo.find_for_upsert_async(model, query, self._info)
        return repo.find_for_upsert(model, query, self._info)

    def get(self, repo: Any, model: type, pk: Any) -> Any:
        if self._async:
            return repo._get_async(model, pk, self._info)
        return repo._get(model, pk, self._info)

    def create(self, repo: Any, model: type, data: dict[str, Any]) -> Any:
        if self._async:
            return repo._create_async(model, data, self._info)
        return repo._create(model, data, self._info)

    def save(self, repo: Any, instance: Any) -> Any:
        if self._async:
            return repo._save_async(instance, self._info)
        return repo._save(instance, self._info)

    def delete(self, repo: Any, instance: Any) -> Any:
        if self._async:
            return repo._delete_async(instance, self._info)
        return repo._delete(instance, self._info)

    # -- Relations -----------------------------------------------------------

    def load_attr(self, instance: Any, name: str) -> Any:
        return self._with_loaded(instance, name, lambda value: value)

    def init_collection(self, instance: Any, name: str) -> None:
        """Mark a just-inserted row's collection as loaded and empty."""
        from sqlalchemy.orm.attributes import set_committed_value

        if name in sa_inspect(instance).unloaded:
            set_committed_value(instance, name, [])

    def link(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            if not self._is_child(parent, spec, child):
                for name, value in self.parent_assignment(parent, spec).items():
                    setattr(child, name, value)
            return None

        def append(collection: Any) -> None:
            if child not in collection:
                collection.append(child)

        return self._with_loaded(parent, spec.name, append)

    def unlink(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            for name in self.parent_assignment(parent, spec):
                setattr(child, name, None)
            return None
        return self._with_loaded(
            parent, spec.name, lambda collection: collection.remove(child)
        )

    def is_member(self, parent: Any, spec: Any, child: Any) -> Any:
        if spec.relation_mode == "reverse_fk":
            return self._is_child(parent, spec, child)
        return self._with_loaded(
            parent, spec.name, lambda collection: child in collection
        )

    def previous_parent(self, parent: Any, spec: Any, child: Any) -> Any:
        """The other row whose reverse foreign key relation holds *child*, if any."""
        from sqlalchemy import select

        if spec.relation_mode != "reverse_fk" or self._is_child(parent, spec, child):
            return None
        keys = {
            parent_key: getattr(child, child_key)
            for parent_key, child_key in self._key_pairs(type(parent), spec)
        }
        if None in keys.values():
            return None
        statement = select(type(parent)).filter_by(**keys)
        if self._async:
            return self._first_async(statement)
        return self._session.execute(statement).scalars().first()

    async def _first_async(self, statement: Any) -> Any:
        result = await self._session.execute(statement)
        return result.scalars().first()

    def back_reference(
        self, parent_model: type, spec: Any
    ) -> tuple[str | None, str | None]:
        """The attribute and foreign key column a nested child points back with."""
        return spec.remote_attr, self._key_pairs(parent_model, spec)[0][1]

    def parent_scope(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        return {
            child_key: getattr(parent, parent_key)
            for parent_key, child_key in self._key_pairs(type(parent), spec)
        }

    def parent_assignment(self, parent: Any, spec: Any) -> dict[str, Any]:
        if spec.relation_mode != "reverse_fk":
            return {}
        if spec.remote_attr is not None:
            return {spec.remote_attr: parent}
        return self.parent_scope(parent, spec)

    # -- Keys ----------------------------------------------------------------

    def pk_value(self, instance: Any) -> Any:
        identity = sa_inspect(instance).identity
        return identity[0] if len(identity) == 1 else identity

    def coerce_pk(self, model: type, value: Any) -> Any:
        from strawberry_orm.repo import _get_sa_pk_column
        from strawberry_orm.upserts import UpsertValidationError

        if not isinstance(value, str):
            return value
        try:
            python_type = _get_sa_pk_column(model).type.python_type
        except NotImplementedError:
            return value
        if python_type is str:
            return value
        try:
            return python_type(value)
        except (TypeError, ValueError):
            raise UpsertValidationError(
                f"{value!r} is not a valid {model.__name__} id"
            ) from None

    # -- Internals -----------------------------------------------------------

    def _with_loaded(self, instance: Any, name: str, then: Callable[[Any], Any]) -> Any:
        if self._async and name in sa_inspect(instance).unloaded:
            return self._with_loaded_async(instance, name, then)
        return then(getattr(instance, name))

    async def _with_loaded_async(
        self, instance: Any, name: str, then: Callable[[Any], Any]
    ) -> Any:
        await self._session.refresh(instance, [name])
        return then(getattr(instance, name))

    def _key_pairs(self, parent_model: type, spec: Any) -> list[tuple[str, str]]:
        prop = sa_inspect(parent_model).relationships[spec.name]
        return [
            (
                prop.parent.get_property_by_column(local).key,
                prop.mapper.get_property_by_column(remote).key,
            )
            for local, remote in prop.local_remote_pairs
        ]

    def _is_child(self, parent: Any, spec: Any, child: Any) -> bool:
        return all(
            getattr(child, child_key) == getattr(parent, parent_key)
            for parent_key, child_key in self._key_pairs(type(parent), spec)
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scoped_sa_relation(
    backend: Any,
    instance: Any,
    parent_model: type,
    fname: str,
    rel_model: type,
    info: Any,
    *,
    uselist: bool = True,
) -> Any:
    """Return the related rows for *fname*, with row scoping applied.

    The optimizer scopes relations when it builds the eager load, but a
    resolver that returns materialized rows never gives it a statement to work
    on. Reading the attribute would then emit an unscoped lazy SELECT and hand
    back rows the caller may not read, so the scope is applied here instead.

    A cache is returned only when this library filled it after applying
    ``scope_rows``. A ``joinedload`` in the resolver is re-queried.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import with_parent

    from strawberry_orm.lazy_resolution import (
        _sqlalchemy_relation_prefetched,
        relation_cache_is_scoped,
    )

    restrict = backend.relation_scope(parent_model, fname, info)
    if _sqlalchemy_relation_prefetched(instance, fname) and (
        restrict is None
        or relation_cache_is_scoped(
            instance,
            fname,
            scope=backend._field_scope(parent_model, fname, info),
            held=_held,
        )
    ):
        return getattr(instance, fname)
    if restrict is None:
        return getattr(instance, fname)

    stmt = select(rel_model).where(with_parent(instance, getattr(parent_model, fname)))
    rows = backend._execute_stmt(restrict(stmt, info), info)
    if uselist:
        return rows
    return rows[0] if rows else None


def _make_sa_on_resolver(
    backend: Any, parent_model: type, field_name: str, relation: str, return_ann: Any
) -> Any:
    """Serve a ``on=`` field on SQLAlchemy.

    Loader options populate the mapped attribute, so there is nowhere to put a
    second view of the same relation. The statement is therefore built per
    parent and left for the batcher to collapse, which is why one of these
    cannot be declared with batching turned off.
    """
    from sqlalchemy import inspect as sa_inspect

    rel = sa_inspect(parent_model).relationships[relation]
    pairs = list(rel.local_remote_pairs or ())
    if rel.secondary is not None or len(pairs) != 1:
        raise ValueError(
            f"{parent_model.__name__}.{field_name}: on={relation!r} goes through "
            f"an association table, so the rows carry no column tying them to one "
            f"parent and the batcher cannot collapse the per-parent queries. The "
            f"field would cost a query per parent row; write it as "
            f"orm.field.lazy instead."
        )

    # The batcher collapses these by rewriting ``fk = <pk>`` into
    # ``fk IN (...)``, and only recognises that predicate with the column on
    # the left. with_parent() emits it the other way round, which reads
    # identically in SQL but silently costs a query per parent.
    fk_column = pairs[0][1]
    rel_model = backend._relation_target_model(parent_model, relation)

    def resolver(self: Any, info: Any) -> Any:
        from sqlalchemy import select

        stmt = select(rel_model).where(fk_column == backend.instance_pk(self))
        restrict = backend.relation_scope(parent_model, field_name, info, on=relation)
        return stmt if restrict is None else restrict(stmt, info)

    resolver.__name__ = field_name
    resolver.__annotations__ = {
        "info": strawberry.types.Info,
        "return": return_ann,
    }
    return resolver


def _make_sa_plain_rel_resolver(
    backend: Any,
    fname: str,
    parent_model: type,
    rel_model: type,
    return_ann: Any,
    *,
    uselist: bool = True,
) -> Any:
    """Create a field for a relation that takes no generated arguments."""

    def resolver(self: Any, info: Any) -> Any:
        return _scoped_sa_relation(
            backend, self, parent_model, fname, rel_model, info, uselist=uselist
        )

    resolver.__name__ = fname
    resolver.__annotations__ = {
        "info": strawberry.types.Info,
        "return": return_ann,
    }
    return strawberry.field(resolver=reads_relation(resolver, fname))


def _make_sa_rel_resolver(
    backend: Any,
    fname: str,
    parent_model: type,
    rel_model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Create a Strawberry field for an SA relationship with filter/order.

    When no filter/order is provided by the caller, falls back to the
    eagerly-loaded attribute.  Otherwise issues a fresh query on
    ``with_parent`` so the database handles the filtering/ordering.
    """
    info_type = strawberry.types.Info

    def _build_stmt(self: Any, info: Any) -> Any:
        from sqlalchemy import select
        from sqlalchemy.orm import with_parent

        stmt = select(rel_model).where(with_parent(self, getattr(parent_model, fname)))
        return backend._apply_nested_queryset(
            stmt,
            parent_model,
            fname,
            rel_model,
            info,
        )

    def _execute(stmt: Any, info: Any) -> Any:
        return backend._execute_stmt(stmt, info)

    if filter_type and order_type:

        def resolver(
            self: Any, info: Any, filter: Any = None, order: Any = None
        ) -> Any:
            if filter is None and order is None:
                return _scoped_sa_relation(
                    backend, self, parent_model, fname, rel_model, info
                )
            stmt = _build_stmt(self, info)
            if filter is not None:
                stmt = backend.apply_filters(stmt, filter, rel_model, info=info)
            if order is not None:
                stmt = backend.apply_ordering(stmt, order, rel_model, info=info)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
            "order": list[order_type] | None,
        }
    elif filter_type:

        def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            if filter is None:
                return _scoped_sa_relation(
                    backend, self, parent_model, fname, rel_model, info
                )
            stmt = _build_stmt(self, info)
            stmt = backend.apply_filters(stmt, filter, rel_model, info=info)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": filter_type | None,
        }
    else:

        def resolver(self: Any, info: Any, order: Any = None) -> Any:
            if order is None:
                return _scoped_sa_relation(
                    backend, self, parent_model, fname, rel_model, info
                )
            stmt = _build_stmt(self, info)
            stmt = backend.apply_ordering(stmt, order, rel_model, info=info)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "order": list[order_type] | None,
        }

    return strawberry.field(resolver=reads_relation(resolver, fname))


_SA_TYPE_MAP: dict[str, type] = {
    "INTEGER": int,
    "SMALLINT": int,
    "BIGINT": int,
    "FLOAT": float,
    "NUMERIC": Decimal,
    "DECIMAL": Decimal,
    "VARCHAR": str,
    "CHAR": str,
    "TEXT": str,
    "BOOLEAN": bool,
    "DATE": datetime.date,
    "TIME": datetime.time,
    "DATETIME": datetime.datetime,
    "TIMESTAMP": datetime.datetime,
    "UUID": str,
    "JSON": str,
    "BLOB": bytes,
}


def _sa_python_type(column_type: Any) -> type:
    """The Python type a column of *column_type* shows as.

    A type is looked up by its class and each class it derives from, so the
    generic ``SmallInteger`` and ``BigInteger`` read as ``Integer`` does. A
    type that wraps another (``impl``) is read as the type it wraps.
    """
    for candidate in (getattr(column_type, "impl", None), column_type):
        for cls in type(candidate).__mro__:
            found = _SA_TYPE_MAP.get(cls.__name__.upper())
            if found is not None:
                return found
    return str


def _get_sa_pk_names(model: type) -> set[str]:
    """Return the set of primary-key column attribute names for an SA model."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    return {mapper.get_property_by_column(col).key for col in mapper.primary_key}


def _get_sa_pk_column(model: type) -> Any:
    """Return the primary-key column attribute for scoped queries."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    return mapper.get_property_by_column(mapper.primary_key[0]).class_attribute


def _get_sa_pk_columns(model: type) -> list[Any]:
    """Every primary-key column attribute of *model*, in key order."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    return [
        mapper.get_property_by_column(column).class_attribute
        for column in mapper.primary_key
    ]


def _whole_key_in(keys: list[Any], rows: Any) -> Any:
    """Whether a row's *keys* are among *rows*, a select of the same columns.

    A key of more than one column is compared as one value: matching on its
    first column alone takes in every row that shares it.
    """
    from sqlalchemy import tuple_

    if len(keys) == 1:
        return keys[0].in_(rows)
    return tuple_(*keys).in_(rows)


def _missing_pk(order_clauses: Any, entity: Any) -> list[Any]:
    """The primary-key columns an order lacks, to end it with.

    Each page is cut by its own query, and a database breaks ties in a
    non-unique order however it likes, differently from one page to the next,
    so a row could land on two pages and another on none. The key is read
    off *entity* itself, so an aliased entity is ordered by its own columns.
    """
    from sqlalchemy.sql import coercions, roles
    from sqlalchemy.sql.util import unwrap_order_by

    mapper = getattr(sa_inspect(entity, raiseerr=False), "mapper", None)
    if mapper is None:
        return []
    ordered = [
        column
        for clause in order_clauses
        for column in unwrap_order_by(coercions.expect(roles.OrderByRole, clause))
    ]
    primary_key = [
        getattr(entity, mapper.get_property_by_column(column).key).expression
        for column in mapper.primary_key
    ]
    return [
        pk
        for pk in primary_key
        if not any(
            pk.shares_lineage(column) and getattr(column, "table", None) is pk.table
            for column in ordered
        )
    ]


#: What a client is told when a statement run by a read fails.
_READ_REFUSED = "Invalid filter expression"


def _sanitized(exc: Any, refused: str = _READ_REFUSED) -> Exception:
    """What a client sees in place of a database error.

    The original names tables and columns, or the database host, and a
    write's carries the row's values, so it is kept only as the cause, for the
    server log. Only an error from running the statement is the request's
    doing: a refused connection ("too many clients") carries no statement, and
    a dropped one is invalidated.
    """
    if exc.statement is None or getattr(exc, "connection_invalidated", False):
        return ConnectionError("Database connection failed")
    return ValueError(refused)


@contextmanager
def _database_errors(refused: str = _READ_REFUSED) -> Generator[None]:
    """Raise what a client may see in place of an error running a statement.

    ``StatementError`` covers the driver's errors and SQLAlchemy's own, such
    as a value the column's type cannot bind; each carries the statement.
    """
    from sqlalchemy.exc import StatementError

    try:
        yield
    except StatementError as exc:
        raise _sanitized(exc, refused) from exc


def _sanitizing(work: Callable[..., Any], *args: Any) -> Any:
    with _database_errors():
        return work(*args)


def _first_appearances_page(query: Any, offset: int, limit: int) -> Any | None:
    """A page of the entities a join to many rows repeats, cut by the database.

    Rows are read with ``.unique()``, which keeps each entity where its first
    row is. Each entity is numbered by that row, so the page counts entities,
    not the rows the join makes of them. ``None`` for a statement whose
    entities cannot be numbered this way: one with a second FROM, more than
    one entity, or loader options that may lean on the join.
    """
    from sqlalchemy import and_, func, select

    from strawberry_orm.lazy_resolution import mark_query_scoped, query_is_scoped

    entity = query.column_descriptions[0]["entity"]
    mapper = getattr(sa_inspect(entity, raiseerr=False), "mapper", None)
    if (
        mapper is None
        or len(query.column_descriptions) != 1
        or len(query.get_final_froms()) != 1
        or query._with_options
    ):
        return None
    keys = [
        getattr(entity, mapper.get_property_by_column(column).key)
        for column in mapper.primary_key
    ]
    order = [*query._order_by_clauses, *_missing_pk(query._order_by_clauses, entity)]
    labels = [f"_orm_key{i}" for i in range(len(keys))]
    numbered = (
        query.with_only_columns(
            *(key.label(label) for key, label in zip(keys, labels, strict=True)),
            func.row_number().over(order_by=order).label("_orm_row"),
        )
        .order_by(None)
        .subquery()
    )
    firsts = (
        select(
            *(numbered.c[label] for label in labels),
            func.min(numbered.c._orm_row).label("_orm_first"),
        )
        .group_by(*(numbered.c[label] for label in labels))
        .subquery()
    )
    page = (
        select(entity)
        .join(
            firsts,
            and_(
                *(
                    key == firsts.c[label]
                    for key, label in zip(keys, labels, strict=True)
                )
            ),
        )
        .order_by(firsts.c._orm_first)
        .offset(offset)
        .limit(limit)
    )
    if query_is_scoped(query):
        mark_query_scoped(page)
    return page


def _may_repeat_rows(stmt: Any) -> bool:
    """True when *stmt* can return one entity on several rows.

    That happens through a join to many rows, a join the ORM cannot see the
    shape of, or a second FROM. A join along a many-to-one relationship
    cannot repeat the row, and neither can a ``DISTINCT`` statement.
    """
    if stmt._distinct:
        return False
    if len(stmt.column_descriptions) != 1 or len(stmt.get_final_froms()) != 1:
        return True
    for target, *_rest in stmt._setup_joins:
        prop = getattr(target, "property", None)
        if getattr(prop, "uselist", True):
            return True
    return False


def _introspect_sa_model(
    model: type,
) -> list[tuple[str, type, bool, type | None]]:
    """Return (field_name, python_type, is_relation, related_model) for each
    column and relationship on an SQLAlchemy mapped class."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    result: list[tuple[str, type, bool, type | None]] = []

    # Named by the attribute, which is what the inputs read through: the
    # database's column name can belong to another attribute, such as a
    # hybrid masking the column it is named after.
    for prop in mapper.column_attrs:
        col = prop.columns[0]
        py_type = _sa_python_type(col.type)

        fk_target = None
        if col.foreign_keys:
            fk = next(iter(col.foreign_keys))
            target_table = fk.column.table if fk.column is not None else None
            if target_table is not None:
                for rel_mapper in mapper.registry.mappers:
                    if rel_mapper.local_table is target_table:
                        fk_target = rel_mapper.class_
                        break

        result.append((prop.key, py_type, False, fk_target))

    for rel in mapper.relationships:
        target_model = rel.mapper.class_
        result.append((rel.key, Any, True, target_model))

    return result


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------

_LOOKUP_TO_SA_OP: dict[str, str] = {
    "exact": "__eq__",
    "neq": "__ne__",
    "gt": "__gt__",
    "gte": "__ge__",
    "lt": "__lt__",
    "lte": "__le__",
    "contains": "contains",
    "i_contains": "icontains",
    "starts_with": "startswith",
    "i_starts_with": "istartswith",
    "ends_with": "endswith",
    "i_ends_with": "iendswith",
}


def _sa_relation_scope_criteria(
    backend: Any, model: type, relation: str, info: Any, query: Any = None
) -> tuple[Any | None, Any]:
    """Criteria limiting *relation* to the rows its type's scope allows.

    A scope that only filters the related table's own columns is applied as
    its WHERE clause. Any other is embedded whole as a subquery over primary
    keys, so joins, limits and anything else ``scope_rows`` adds are carried
    across intact, and *query*, the statement the criteria go in, comes back
    carrying its ``with_loader_criteria``. Any ``scope=`` on this edge is
    applied too, so a filter cannot reach rows the read path hides.
    """
    if backend is None:
        return None, query
    get_qs = backend.relation_scope(model, relation, info)
    if get_qs is None:
        return None, query

    from sqlalchemy import select as sa_select

    related = backend._relation_target_model(model, relation)
    scoped = get_qs(sa_select(related), info)
    lifted = _liftable_criteria(scoped, related, model)
    if lifted is not None:
        return lifted, query
    keys = _get_sa_pk_columns(related)
    return (
        _whole_key_in(keys, scoped.with_only_columns(*keys)),
        _carrying(query, scoped),
    )


def _unordered(query: Any) -> Any:
    """*query* without its ORDER BY, when the order cannot change which rows it has.

    It can with a limit, and with PostgreSQL's ``DISTINCT ON``, which keeps the
    first row of each group in that order.
    """
    if (
        query._limit_clause is not None
        or query._offset_clause is not None
        or query._fetch_clause is not None
        or query._distinct_on
    ):
        return query
    return query.order_by(None)


def _carrying(outer: Any, *embedded: Any) -> Any:
    """*outer* with the ``with_loader_criteria`` of statements embedded in it.

    SQLAlchemy applies those criteria from the outermost statement only, to
    every place their entity is read in it, so the rows a scope keeps through
    them are lost once the scope runs inside another statement.
    """
    from sqlalchemy.orm.util import LoaderCriteriaOption

    held = {id(option) for option in getattr(outer, "_with_options", ())}
    carried = []
    for stmt in embedded:
        for option in getattr(stmt, "_with_options", ()):
            if isinstance(option, LoaderCriteriaOption) and id(option) not in held:
                held.add(id(option))
                carried.append(option)
    return outer.options(*carried) if carried else outer


def _liftable_criteria(scoped: Any, related: type, model: type) -> Any | None:
    """The scope's WHERE clause, when it can stand in for the whole statement.

    Inside a correlated EXISTS, ``pk IN (SELECT pk ... WHERE scope)`` is
    re-evaluated for every outer row on SQLite. A scope that only filters the
    related table's own columns means the same thing as its WHERE clause, so
    that is used instead. Anything more - joins, limits, grouping, nested
    selects, raw SQL, a relation back to the same table, or criteria the WHERE
    clause does not hold - keeps the subquery.
    """
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(related)
    if (
        mapper.local_table is sa_inspect(model).local_table
        # The discriminator of a class sharing its table with others is added
        # when the ORM compiles the statement, not in its WHERE clause.
        or mapper.single
        or len(mapper.tables) != 1
        or not _says_only_where(scoped, related)
    ):
        return None
    return scoped.whereclause


def _says_only_where(scoped: Any, related: type) -> bool:
    """Whether *scoped* is ``select(related)`` narrowed by a WHERE clause alone.

    Criteria on *related*'s own tables mean the same wherever they are put.
    Anything more - joins, limits, grouping, nested selects, raw SQL, or
    criteria the WHERE clause does not hold, such as ``with_loader_criteria``
    and other options the ORM applies when it compiles the statement - does
    not.
    """
    from sqlalchemy import Select, select
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import TextClause
    from sqlalchemy.sql.selectable import SelectBase

    if not isinstance(scoped, Select) or (
        scoped._limit_clause is not None
        or scoped._offset_clause is not None
        or scoped._group_by_clauses
        or scoped._having_criteria
        or scoped._distinct
        or scoped._setup_joins
        or scoped._from_obj
        or scoped._with_options
        or [d.get("entity") for d in scoped.column_descriptions] != [related]
        or scoped.get_final_froms() != select(related).get_final_froms()
    ):
        return False
    tables = sa_inspect(related).tables
    where = scoped.whereclause
    return where is None or not any(
        isinstance(element, (SelectBase, TextClause))
        or not any(getattr(element, "table", tables[0]) is table for table in tables)
        for element in visitors.iterate(where)
    )


def _build_sa_filter(
    filter_input: Any,
    model: type,
    *,
    query: Any = None,
    info: Any = None,
    backend: Any = None,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
    _depth: int = 0,
    _on_query: bool = True,
) -> tuple[Any, Any]:
    """Return ``(clause | None, query)``.

    A ``@filter_field`` narrows *query* itself only while the filter so far
    is a plain AND at the top (``_on_query``). Anywhere else - under
    ``not:``, ``any:``, ``oneOf:`` or ``object:`` - it becomes a clause.
    """
    from sqlalchemy import and_, not_, or_

    if _depth > max_depth:
        raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")

    if filter_input is None or filter_input is strawberry.UNSET:
        return None, query

    fields = filter_input.__class__.__dataclass_fields__
    custom_filters = getattr(type(filter_input), "_custom_filters", {})
    recurse_kw = dict(
        query=query,
        info=info,
        backend=backend,
        max_depth=max_depth,
        max_branches=max_branches,
        enable_regex=enable_regex,
        max_in_list_size=max_in_list_size,
        _depth=_depth + 1,
        _on_query=False,
    )

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "is_null":
            from strawberry_orm.backends._base import _FILTER_RELATION_PRESENCE_ERROR

            raise ValueError(_FILTER_RELATION_PRESENCE_ERROR)
        elif key == "field":
            clause = _build_sa_field_clause(
                val,
                model,
                enable_regex=enable_regex,
                max_in_list_size=max_in_list_size,
            )
            return clause, query
        elif key == "object":
            obj_fields = val.__class__.__dataclass_fields__
            for rel_name in obj_fields:
                nested_filter = getattr(val, rel_name)
                if nested_filter is strawberry.UNSET or nested_filter is None:
                    continue
                relationship_prop = getattr(model, rel_name)
                # Traversal reaches the related table directly, so the related
                # type's row scoping has to be re-applied here or it is bypassed.
                scope_criteria, query = _sa_relation_scope_criteria(
                    backend, model, rel_name, info, query
                )
                is_null_val = getattr(nested_filter, "is_null", strawberry.UNSET)
                if is_null_val is not strawberry.UNSET and is_null_val is not None:
                    if relationship_prop.property.uselist:
                        # "Has none" must mean "has none the caller can see".
                        present = (
                            relationship_prop.any()
                            if scope_criteria is None
                            else relationship_prop.any(scope_criteria)
                        )
                        return (not_(present) if is_null_val else present), query
                    if scope_criteria is not None:
                        # "Has a related row" must mean "has a *visible* one".
                        present = relationship_prop.has(scope_criteria)
                        return (not_(present) if is_null_val else present), query
                    local_col = list(relationship_prop.property.local_columns)[0]
                    clause = (
                        local_col.is_(None) if is_null_val else local_col.isnot(None)
                    )
                    return clause, query
                rel_model = relationship_prop.property.mapper.class_
                reference = _sa_reference_key(relationship_prop)
                if reference is not None:
                    local_col, key = reference
                    fk_clause = build_reference_object_filter_clause(
                        nested_filter,
                        key=key,
                        build_field_clause=_build_sa_reference_field_clause,
                        custom_filter_keys=frozenset(custom_filters.keys()),
                        max_branches=max_branches,
                        max_depth=max_depth,
                        depth=_depth + 1,
                        local_col=local_col,
                        enable_regex=enable_regex,
                        max_in_list_size=max_in_list_size,
                    )
                    if fk_clause is not None:
                        if scope_criteria is not None:
                            fk_clause = and_(
                                fk_clause, relationship_prop.has(scope_criteria)
                            )
                        return fk_clause, query
                with past_a_hop():
                    inner, query = _build_sa_filter(
                        nested_filter,
                        rel_model,
                        **{**recurse_kw, "query": query},
                    )
                if inner is not None:
                    if scope_criteria is not None:
                        inner = and_(inner, scope_criteria)
                    if relationship_prop.property.uselist:
                        return relationship_prop.any(inner), query
                    else:
                        return relationship_prop.has(inner), query
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_sa_filter(
                    f, model, **{**recurse_kw, "query": query, "_on_query": _on_query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            return (and_(*clauses) if clauses else None), query
        elif key == "any":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_sa_filter(
                    f, model, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            return (or_(*clauses) if clauses else None), query
        elif key == "not_":
            inner, query = _build_sa_filter(
                val, model, **{**recurse_kw, "query": query}
            )
            return (not_(inner) if inner is not None else None), query
        elif key == "one_of":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            clauses = []
            for f in val:
                sub_clause, query = _build_sa_filter(
                    f, model, **{**recurse_kw, "query": query}
                )
                if sub_clause is not None:
                    clauses.append(sub_clause)
            return (or_(*clauses) if clauses else None), query
        elif key in custom_filters:
            if not _on_query:
                clause = _sa_custom_filter_clause(
                    custom_filters[key], filter_input, model, val, info
                )
                return clause, query
            query = invoke_custom_callback(
                custom_filters[key],
                filter_input,
                query=query,
                value=val,
                info=info,
            )
            return None, query

    return None, query


def _sa_custom_filter_clause(
    handler: Any, filter_input: Any, model: type, value: Any, info: Any
) -> Any:
    """A ``@filter_field`` as a condition on rows of *model*.

    The handler narrows a query of its own over *model*, and the rows it
    keeps are matched by key, so the condition can be negated, made one
    alternative of several, or tested on a related row.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select as sa_select

    narrowed = invoke_custom_callback(
        handler, filter_input, query=sa_select(model), value=value, info=info
    )
    keys = list(sa_inspect(model).primary_key)
    return _whole_key_in(keys, narrowed.with_only_columns(*keys).correlate(None))


def _sa_reference_key(relationship_prop: Any) -> tuple[Any, str] | None:
    """The parent's foreign key column and the related key it holds.

    Only a many-to-one whose foreign key holds the related primary key: the
    key is then on the parent's own row, and comparing it needs no join.
    """
    from sqlalchemy.orm import MANYTOONE

    prop = relationship_prop.property
    if prop.direction is not MANYTOONE or len(prop.local_remote_pairs) != 1:
        return None
    local_col, remote_col = prop.local_remote_pairs[0]
    primary_key = prop.mapper.primary_key
    if len(primary_key) != 1 or primary_key[0] is not remote_col:
        return None
    return local_col, prop.mapper.get_property_by_column(remote_col).key


def _build_sa_field_clause(
    field_input: Any,
    model: type,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    """Translate a *Field input (e.g. UserField) into column-level conditions."""
    from sqlalchemy import and_

    clauses = []
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue

        column = getattr(model, col_name, None)
        if column is None:
            continue

        col_clauses = _build_lookup_clauses(
            column,
            lookup,
            enable_regex=enable_regex,
            max_in_list_size=max_in_list_size,
        )
        clauses.extend(col_clauses)

    if not clauses:
        return None
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def _escape_like(val: str) -> str:
    """Escape SQL LIKE wildcards so they are treated as literals."""
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _coerce_reference_value(column: Any, val: Any) -> Any:
    """*val*, an ID, as *column* holds it; never guessed from how it reads.

    ``"00123"`` keys a text column as itself, not as the integer 123.
    """
    if isinstance(val, list):
        return [_coerce_reference_value(column, item) for item in val]
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        return val
    if python_type not in (int, uuid.UUID) or isinstance(val, python_type):
        return val
    try:
        return python_type(str(val))
    except ValueError:
        raise ValueError(f"{val!r} is not a valid ID.") from None


def _build_reference_lookup_clauses(
    column: Any,
    lookup: Any,
    *,
    max_in_list_size: int = 500,
) -> list[Any]:
    if not is_reference_lookup(lookup):
        raise TypeError("Expected ReferenceLookup for FK / reference filtering.")

    clauses = []
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            clauses.append(column.is_(None) if val else column.isnot(None))
            continue
        val = _coerce_reference_value(column, val)
        if op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                clauses.append(column.in_(val))
            else:
                clauses.append(column.notin_(val))
        elif op_name == "neq":
            clauses.append(column != val)
        elif op_name == "exact":
            clauses.append(column == val)

    return clauses


def _build_sa_reference_field_clause(
    field_input: Any,
    *,
    local_col: Any,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from sqlalchemy import and_

    clauses: list[Any] = []
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue
        if is_reference_lookup(lookup):
            clauses.extend(
                _build_reference_lookup_clauses(
                    local_col,
                    lookup,
                    max_in_list_size=max_in_list_size,
                )
            )
        elif is_fk_shortcut_lookup(lookup):
            clauses.extend(
                _build_lookup_clauses(
                    local_col,
                    lookup,
                    enable_regex=enable_regex,
                    max_in_list_size=max_in_list_size,
                )
            )
        else:
            raise TypeError(
                f"Expected ReferenceLookup or FK-mappable IntComparisonLookup "
                f"for reference field '{col_name}'."
            )

    return and_(*clauses) if clauses else None


def _build_lookup_clauses(
    column: Any,
    lookup: Any,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> list[Any]:
    """Translate a single lookup object (e.g. StringLookup) into clauses."""
    if is_reference_lookup(lookup):
        return _build_reference_lookup_clauses(
            column,
            lookup,
            max_in_list_size=max_in_list_size,
        )

    clauses = []
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            clauses.append(column.is_(None) if val else column.isnot(None))
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            values = [_column_value(column, item) for item in val]
            if op_name == "in_list":
                clauses.append(column.in_(values))
            else:
                clauses.append(column.notin_(values))
        elif op_name == "range":
            clauses.append(
                column.between(
                    _column_value(column, val.start), _column_value(column, val.end)
                )
            )
        elif op_name == "contains":
            clauses.append(column.contains(val, autoescape=True))
        elif op_name == "i_contains":
            clauses.append(column.ilike(f"%{_escape_like(val)}%", escape="\\"))
        elif op_name == "starts_with":
            clauses.append(column.startswith(val, autoescape=True))
        elif op_name == "i_starts_with":
            clauses.append(column.ilike(f"{_escape_like(val)}%", escape="\\"))
        elif op_name == "ends_with":
            clauses.append(column.endswith(val, autoescape=True))
        elif op_name == "i_ends_with":
            clauses.append(column.ilike(f"%{_escape_like(val)}", escape="\\"))
        elif op_name == "regex":
            if not enable_regex:
                raise ValueError(
                    "Regex filters are disabled. Pass enable_regex_filters=True to enable."
                )
            clauses.append(column.regexp_match(val))
        elif op_name == "i_regex":
            if not enable_regex:
                raise ValueError(
                    "Regex filters are disabled. Pass enable_regex_filters=True to enable."
                )
            clauses.append(column.regexp_match(val, flags="i"))
        elif op_name in ("exact", "neq", "gt", "gte", "lt", "lte"):
            sa_op = _LOOKUP_TO_SA_OP[op_name]
            clauses.append(getattr(column, sa_op)(_column_value(column, val)))

    return clauses


def _column_value(column: Any, value: Any) -> Any:
    """*value* as *column* holds it.

    Date and time lookups take ISO 8601 text. Compared as text, the value is
    bound as ``VARCHAR``, which PostgreSQL will not compare with a date; read
    as a ``date``, ``datetime`` or ``time``, it is bound in the column's type.
    """
    column_type = getattr(column, "type", None)
    if not isinstance(value, str) or column_type is None:
        return value
    python_type = _sa_python_type(column_type)
    try:
        if python_type is datetime.datetime:
            return datetime.datetime.fromisoformat(value)
        if python_type is datetime.date:
            try:
                return datetime.date.fromisoformat(value)
            except ValueError:
                return datetime.datetime.fromisoformat(value).date()
        if python_type is datetime.time:
            return datetime.time.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{value!r} is not an ISO 8601 {python_type.__name__}."
        ) from None
    return value


# ---------------------------------------------------------------------------
# Ordering translation
# ---------------------------------------------------------------------------


def _build_sa_ordering(
    order_input: Any,
    model: type,
    *,
    query: Any = None,
    info: Any = None,
    backend: Any = None,
) -> tuple[list[Any], list[Any], Any]:
    """Return ``(clauses, joins, query)``."""
    clauses: list[Any] = []
    joins: list[Any] = []
    fields = order_input.__class__.__dataclass_fields__
    custom_orders = getattr(type(order_input), "_custom_orders", {})

    for key in fields:
        val = getattr(order_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            clauses.extend(_build_sa_order_field(val, model))
        elif key == "object":
            obj_fields = val.__class__.__dataclass_fields__
            for rel_name in obj_fields:
                nested = getattr(val, rel_name)
                if nested is strawberry.UNSET or nested is None:
                    continue
                if backend is not None:
                    backend.reject_scoped_order_traversal(
                        model, rel_name, type(order_input)
                    )
                relationship_prop = getattr(model, rel_name)
                rel_model = relationship_prop.property.mapper.class_
                joins.append(relationship_prop)
                sub_clauses, sub_joins, query = _build_sa_ordering(
                    nested, rel_model, query=query, info=info, backend=backend
                )
                clauses.extend(sub_clauses)
                joins.extend(sub_joins)
        elif key in custom_orders:
            query = invoke_custom_callback(
                custom_orders[key],
                order_input,
                query=query,
                value=val,
                info=info,
            )

    return clauses, joins, query


def _truncate(column: Any, interval: Any, dialect: str) -> Any:
    """*column* cut down to the start of its *interval*.

    PostgreSQL cuts a ``timestamptz`` in the session's time zone. SQLite keeps
    datetimes as text, so the start of the period is rendered as ISO text.
    """
    from sqlalchemy import Date, DateTime, Integer, cast, func

    from strawberry_orm.grouping import SQLITE_PERIOD_START

    is_datetime = isinstance(column.type, DateTime)
    if dialect.startswith("postgres"):
        truncated = func.date_trunc(interval.value.lower(), column)
        return truncated if is_datetime else cast(truncated, Date)
    if dialect == "sqlite":
        fmt = "%Y-%m-%dT%H:%M:%S" if is_datetime else "%Y-%m-%d"
        if interval is DateGroupByInterval.QUARTER:
            months_in = (cast(func.strftime("%m", column), Integer) - 1) % 3
            modifiers: tuple[Any, ...] = (
                "start of month",
                func.printf("-%d months", months_in),
            )
        else:
            modifiers = SQLITE_PERIOD_START[interval]
        return func.strftime(fmt, column, *modifiers)
    raise ValueError(
        f"Grouping by a date interval is supported on PostgreSQL and SQLite, "
        f"not {dialect}."
    )


def _build_sa_agg_cols(
    model: type,
    requested: dict[str, Any],
    *,
    custom_fields: list[tuple[str, Any, type]] | None = None,
    info: Any = None,
    dialect: str = "postgresql",
) -> list[Any]:
    """Build aggregate column expressions based on requested aggregates."""
    from sqlalchemy import func

    agg_cols: list[Any] = []
    if requested.get("count"):
        agg_cols.append(func.count().label("_count"))
    for func_name in _AGGREGATES:
        for fname in requested.get(func_name, []):
            col = getattr(model, fname, None)
            if col is not None:
                agg_cols.append(
                    _sa_aggregate(func_name, col, dialect).label(
                        f"_{func_name}_{fname}"
                    )
                )
    if custom_fields:
        columns = model.__table__.c  # type: ignore[attr-defined]
        for field_name, handler, _rtype in custom_fields:
            expr = _invoke_aggregate_handler(handler, columns, info=info)
            if expr is not None:
                agg_cols.append(expr.label(f"_custom_{field_name}"))
    return agg_cols


def _order_parts(clause: Any) -> tuple[Any, bool, str | None]:
    """``(expression, descending, nulls)`` for one ORDER BY clause."""
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.elements import UnaryExpression

    descending = False
    nulls: str | None = None
    while isinstance(clause, UnaryExpression) and clause.modifier in (
        operators.asc_op,
        operators.desc_op,
        operators.nulls_first_op,
        operators.nulls_last_op,
    ):
        if clause.modifier is operators.desc_op:
            descending = True
        elif clause.modifier is operators.nulls_first_op:
            nulls = "first"
        elif clause.modifier is operators.nulls_last_op:
            nulls = "last"
        clause = clause.element
    return clause, descending, nulls


def _sa_connection_ordering(
    query: Any,
    order_input: Any,
    model: type,
    *,
    backend: Any = None,
    info: Any = None,
) -> tuple[list[Any], list[Any], list[Any]]:
    """The order the connection reads its rows in, the joins it needs, and the
    statements whose row criteria the ordered statement has to carry.

    The client's ``order`` when given, else the query's own. An order through
    a to-many relation is left out: its join would repeat rows. The set an
    ``objects:`` reads is one value per row, and stays. A query's own
    clauses are kept only when they read the model's table, the one thing a
    select over the model's rows is sure to have.
    """
    from strawberry_orm.grouping import SetOrder, order_terms

    if not order_input:
        table = model.__table__  # type: ignore[attr-defined]
        return (
            [
                clause
                for clause in query._order_by_clauses
                if getattr(_order_parts(clause)[0], "table", None) is table
            ],
            [],
            [],
        )
    clauses: list[Any] = []
    joins: list[Any] = []
    carried: list[Any] = []
    for term in order_terms(backend, model, order_input):
        if isinstance(term, SetOrder):
            clause, rows = backend._set_order(model, term, info)
            clauses.append(clause)
            carried.append(rows)
        else:
            _sa_order_clauses(term, model, (), clauses, joins, backend=backend)
    return clauses, joins, carried


def _sa_order_clauses(
    order_input: Any,
    model: type,
    path: tuple[Any, ...],
    clauses: list[Any],
    joins: list[Any],
    *,
    backend: Any,
) -> None:
    """Collect one order input's column clauses, and the joins they need."""
    for key in order_input.__class__.__dataclass_fields__:
        value = getattr(order_input, key)
        if value is strawberry.UNSET or value is None:
            continue
        if key == "field":
            if path:
                joins.extend(join for join in path if join not in joins)
            clauses.extend(_build_sa_order_field(value, model))
        elif key == "object":
            for relation in value.__class__.__dataclass_fields__:
                nested = getattr(value, relation)
                if nested is strawberry.UNSET or nested is None:
                    continue
                backend.reject_scoped_order_traversal(
                    model, relation, type(order_input)
                )
                join = getattr(model, relation)
                if join.property.uselist:
                    continue
                _sa_order_clauses(
                    nested,
                    join.property.mapper.class_,
                    (*path, join),
                    clauses,
                    joins,
                    backend=backend,
                )


def _sa_aggregate(name: str, column: Any, dialect: str) -> Any:
    """The SQL aggregate *name* of *column*."""
    from sqlalchemy import Float, func, type_coerce

    if name == "median":
        if not dialect.startswith("postgres"):
            raise ValueError(f"median is supported on PostgreSQL, not {dialect}.")
        return type_coerce(func.percentile_cont(0.5).within_group(column), Float)
    return getattr(func, name)(column)


def _sa_group_ordering(clauses: list[Any], dialect: str) -> tuple[list[Any], list[Any]]:
    """Order groups as their first rows come in the connection's order.

    Read in that order, a group starts at its least value of each clause, or
    its greatest for a descending one, so groups are ranked by those. A
    clause on a column the database cannot take the least of is left out.
    """
    from sqlalchemy import func

    firsts: list[Any] = []
    ordering: list[Any] = []
    for clause in clauses:
        expr, descending, nulls = _order_parts(clause)
        expr = _sa_rankable(expr, dialect)
        if expr is None:
            continue
        name = f"_o{len(firsts)}"
        first = (func.max if descending else func.min)(expr).label(name)
        ranked = first.desc() if descending else first.asc()
        if nulls == "first":
            ranked = ranked.nulls_first()
        elif nulls == "last":
            ranked = ranked.nulls_last()
        firsts.append(first)
        ordering.append(ranked)
    return firsts, ordering


def _sa_rankable(expr: Any, dialect: str) -> Any:
    """*expr* as a value PostgreSQL has a ``MIN`` and ``MAX`` of, or ``None``.

    PostgreSQL has neither for a UUID nor a boolean. A UUID's text sorts as
    the UUID does, and a boolean's integer as the boolean does, false first.
    A type is judged as it is stored on *dialect*: a ``TypeDecorator`` may
    keep a UUID as text on one database and as a UUID on another.
    """
    from sqlalchemy import Integer, String, TypeDecorator, cast

    column_type = expr.type.dialect_impl(_sa_dialect(dialect))
    while isinstance(column_type, TypeDecorator):
        column_type = column_type.impl
    kinds = {cls.__name__ for cls in type(column_type).__mro__}
    if "Uuid" in kinds:
        return cast(expr, String)
    if "Boolean" in kinds:
        return cast(expr, Integer)
    if kinds & _SA_UNRANKABLE_TYPES:
        return None
    return expr


@functools.cache
def _sa_dialect(name: str) -> Any:
    """The SQLAlchemy dialect *name* configures, or a generic one."""
    from sqlalchemy.engine import default, make_url
    from sqlalchemy.exc import ArgumentError, NoSuchModuleError

    if name.startswith("postgres"):
        name = "postgresql"
    try:
        return make_url(f"{name}://").get_dialect()()
    except (ArgumentError, NoSuchModuleError):
        return default.DefaultDialect()


def _build_sa_order_field(field_input: Any, model: type) -> list[Any]:
    from sqlalchemy import asc, desc

    clauses: list[Any] = []
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        direction = getattr(field_input, col_name)
        if direction is strawberry.UNSET or direction is None:
            continue

        column = getattr(model, col_name, None)
        if column is None:
            continue

        dir_value = direction.value if hasattr(direction, "value") else str(direction)

        if dir_value.startswith("ASC"):
            clause = asc(column)
            if "NULLS_FIRST" in dir_value:
                clause = clause.nullsfirst()
            elif "NULLS_LAST" in dir_value:
                clause = clause.nullslast()
        else:
            clause = desc(column)
            if "NULLS_FIRST" in dir_value:
                clause = clause.nullsfirst()
            elif "NULLS_LAST" in dir_value:
                clause = clause.nullslast()

        clauses.append(clause)

    return clauses
