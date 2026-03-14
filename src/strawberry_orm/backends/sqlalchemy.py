"""SQLAlchemy backend -- built from scratch using SQLAlchemy introspection."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Callable, Optional

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm.filters import TYPE_TO_LOOKUP
from strawberry_orm.mutations import make_ref_type
from strawberry_orm.optimizer import OptimizerExtension, OptimizerStore
from strawberry_orm.types import Ordering


class SQLAlchemyBackend:
    """Backend adapter for SQLAlchemy."""

    def __init__(
        self,
        *,
        dialect: str = "postgresql",
        session_getter: Callable[..., Any] | None = None,
        filter_overrides: dict[type, type] | None = None,
        **kwargs: Any,
    ) -> None:
        self._dialect = dialect
        self._session_getter = session_getter
        self._filter_overrides = filter_overrides or {}
        self._store = OptimizerStore()
        self._type_registry: dict[str, type] = {}
        self._type_querysets: dict[type, Callable[..., Any]] = {}

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        backend = self
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")

        def decorator(cls: type) -> Any:
            from sqlalchemy import inspect as sa_inspect
            from strawberry_orm.types import FieldDefinition
            from strawberry_orm.optimizer.store import FieldHints

            mapper = sa_inspect(model)

            col_types: dict[str, type] = {}
            for col in mapper.columns:
                col_type_name = type(col.type).__name__.upper()
                py_type = _SA_TYPE_MAP.get(col_type_name, str)
                if hasattr(col.type, "impl"):
                    impl_name = type(col.type.impl).__name__.upper()
                    py_type = _SA_TYPE_MAP.get(impl_name, py_type)
                if col.nullable:
                    py_type = Optional[py_type]
                col_types[col.key] = py_type

            rel_names = {rel.key for rel in mapper.relationships}

            annotations = getattr(cls, "__annotations__", {}).copy()

            for field_name in list(annotations):
                if include and field_name not in include:
                    continue
                if exclude and field_name in exclude:
                    del annotations[field_name]
                    continue

                ann = annotations[field_name]
                if ann is strawberry.auto:
                    if field_name in col_types:
                        annotations[field_name] = col_types[field_name]

            cls.__annotations__ = annotations
            cls.__orm_model__ = model  # type: ignore[attr-defined]

            if filters is not None:
                cls.__orm_filter__ = filters  # type: ignore[attr-defined]
            if order is not None:
                cls.__orm_order__ = order  # type: ignore[attr-defined]

            type_name = name or cls.__name__

            if hasattr(cls, "get_queryset") and isinstance(
                vars(cls).get("get_queryset"), classmethod
            ):
                backend._type_querysets[model] = cls.get_queryset

            for attr_name in list(vars(cls)):
                val = getattr(cls, attr_name, None)
                if isinstance(val, FieldDefinition):
                    hints = FieldHints(
                        load=val.load,
                        only=val.only,
                        compute=val.compute,
                        disable_optimization=val.disable_optimization,
                    )
                    backend._store.register(type_name, attr_name, hints)
                    try:
                        delattr(cls, attr_name)
                    except AttributeError:
                        pass
                elif getattr(val, "_orm_auto_field", False):
                    try:
                        delattr(cls, attr_name)
                    except AttributeError:
                        pass

            for field_name, ann in annotations.items():
                if field_name in vars(cls):
                    continue
                el_type = _extract_element_type(ann)
                if el_type is None:
                    continue
                f_type = getattr(el_type, "__orm_filter__", None)
                o_type = getattr(el_type, "__orm_order__", None)
                if f_type is None and o_type is None:
                    continue
                if field_name not in rel_names:
                    continue
                rel = mapper.relationships[field_name]
                rel_model = rel.mapper.class_
                setattr(cls, field_name, _make_sa_rel_resolver(
                    backend, field_name, model, rel_model, f_type, o_type,
                ))

            result = strawberry.type(cls, name=name if name else None)

            backend._type_registry[type_name] = model

            return result

        return decorator

    def input(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")

        fields_meta = _introspect_sa_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[ftype]
            defaults[fname] = strawberry.UNSET

        type_name = name or f"{model.__name__}Input"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        return strawberry.input(cls)

    def partial(self, model: type, **kwargs: Any) -> Any:
        kwargs.setdefault("name", f"{model.__name__}PartialInput")
        return self.input(model, **kwargs)

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = _introspect_sa_model(model)

        field_annotations: dict[str, Any] = {}
        field_defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            lookup_type = self._filter_overrides.get(ftype) or TYPE_TO_LOOKUP.get(ftype)
            if lookup_type is not None:
                field_annotations[fname] = Optional[lookup_type]
                field_defaults[fname] = strawberry.UNSET

        field_type_name = f"{model.__name__}Field"
        field_ns: dict[str, Any] = {
            "__annotations__": field_annotations,
            **field_defaults,
        }
        field_cls = type(field_type_name, (), field_ns)
        FieldType = strawberry.input(field_cls, one_of=True)

        filter_type_name = f"{model.__name__}Filter"

        filter_ns: dict[str, Any] = {
            "__annotations__": {"field": Optional[FieldType]},
            "field": strawberry.UNSET,
        }
        FilterCls = type(filter_type_name, (), filter_ns)

        FilterCls.__annotations__["all"] = Optional[list[FilterCls]]
        FilterCls.__annotations__["any"] = Optional[list[FilterCls]]
        FilterCls.__annotations__["not_"] = Optional[FilterCls]
        FilterCls.__annotations__["one_of"] = Optional[list[FilterCls]]
        FilterCls.all = strawberry.UNSET
        FilterCls.any = strawberry.UNSET
        FilterCls.not_ = strawberry.field(default=strawberry.UNSET, name="not")
        FilterCls.one_of = strawberry.UNSET

        FilterType = strawberry.input(FilterCls, one_of=True)
        FilterType._field_type = FieldType  # type: ignore[attr-defined]
        FilterType.__orm_model__ = model  # type: ignore[attr-defined]
        return FilterType

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        model = model_or_type
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")

        fields_meta = _introspect_sa_model(model)
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}

        for fname, ftype, is_relation, _rel_model in fields_meta:
            if include and fname not in include:
                continue
            if exclude and fname in exclude:
                continue
            if is_relation:
                continue
            annotations[fname] = Optional[Ordering]
            defaults[fname] = strawberry.UNSET

        type_name = f"{model.__name__}Order"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        result = strawberry.input(cls)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        return result

    def aggregate(self, model: type, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "aggregate() is not yet supported for the SQLAlchemy backend"
        )

    # -- Fields --------------------------------------------------------------

    def field(self, **kwargs: Any) -> Any:
        from strawberry_orm.types import FieldDefinition
        hint_keys = {"load", "only", "compute", "disable_optimization"}
        if hint_keys & set(kwargs):
            return FieldDefinition(
                load=kwargs.get("load"),
                only=kwargs.get("only"),
                compute=kwargs.get("compute"),
                disable_optimization=kwargs.get("disable_optimization", False),
                description=kwargs.get("description"),
            )
        allowed = {"description", "deprecation_reason", "default", "resolver", "name"}
        return strawberry.field(**{k: v for k, v in kwargs.items() if k in allowed})

    def node(self, **kwargs: Any) -> Any:
        return strawberry.field(**kwargs)

    def connection(self, **kwargs: Any) -> Any:
        allowed = {"description", "deprecation_reason", "default", "resolver", "name"}
        return strawberry.field(**{k: v for k, v in kwargs.items() if k in allowed})

    # -- Mutations -----------------------------------------------------------

    def create(self, input_type: type, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    def update(self, input_type: type, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    def delete(self, **kwargs: Any) -> Any:
        return strawberry.field(description=kwargs.get("description"))

    # -- Related list refs ---------------------------------------------------

    def ref(
        self,
        model: type,
        *,
        create: type | None = None,
        update: type | None = None,
        delete: bool = False,
    ) -> type:
        return make_ref_type(model, create=create, update=update, delete=delete)

    def apply_ref_list(
        self, instance: Any, field: str, refs: list[Any], info: Any
    ) -> None:
        from sqlalchemy.orm import Session

        session: Session = self._get_session(info)
        relationship = getattr(type(instance), field).property
        target_model = relationship.mapper.class_

        new_related: list[Any] = []
        for ref in refs:
            ref_id = getattr(ref, "id", strawberry.UNSET)
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_id is not strawberry.UNSET and ref_id is not None:
                obj = session.get(target_model, ref_id)
                if obj is not None:
                    new_related.append(obj)
            elif ref_create is not strawberry.UNSET and ref_create is not None:
                obj = target_model(**_input_to_dict(ref_create))
                session.add(obj)
                new_related.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = _input_to_dict(ref_update)
                pk = data.pop("id")
                obj = session.get(target_model, pk)
                if obj is not None:
                    for k, v in data.items():
                        setattr(obj, k, v)
                    new_related.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                obj = session.get(target_model, ref_delete.id)
                if obj is not None:
                    session.delete(obj)

        setattr(instance, field, new_related)

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        clause = _build_sa_filter(filter_input, model)
        if clause is not None:
            query = query.where(clause)
        return query

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        clauses = _build_sa_ordering(order_input, model)
        if clauses:
            query = query.order_by(*clauses)
        return query

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        from sqlalchemy import select
        return select(model)

    def is_query_object(self, value: Any) -> bool:
        try:
            from sqlalchemy.sql import Select
            return isinstance(value, Select)
        except ImportError:
            return False

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def apply_optimizer_hints(
        self, store: Any, query: Any, info: Any
    ) -> Any:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.orm import joinedload, selectinload, load_only

        try:
            entity = query.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError):
            return query

        mapper = sa_inspect(entity)

        get_qs = self._type_querysets.get(entity)
        if get_qs is not None:
            query = get_qs(query, info)

        def _get_field_name(node: Any) -> str:
            """Extract field name from a GraphQL FieldNode, converting camelCase
            to snake_case to match ORM attribute names."""
            name = node.name.value
            import re
            return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

        def _apply_loads(stmt: Any, selection_set: Any, current_mapper: Any) -> Any:
            if selection_set is None:
                return stmt
            for node in selection_set.selections:
                field_name = _get_field_name(node)

                if field_name in current_mapper.relationships:
                    rel = current_mapper.relationships[field_name]
                    rel_attr = getattr(current_mapper.entity, field_name)
                    if rel.uselist:
                        base_loader = selectinload(rel_attr)
                    else:
                        base_loader = joinedload(rel_attr)

                    if node.selection_set:
                        child_mapper = sa_inspect(rel.mapper.class_)
                        nested = _collect_nested_loaders(base_loader, node.selection_set, child_mapper)
                        for nl in nested:
                            stmt = stmt.options(nl)
                    else:
                        stmt = stmt.options(base_loader)

                type_name = self._type_name_for_model(current_mapper.entity)
                if type_name and store:
                    hints = store.get(type_name, field_name)
                    if hints and not hints.disable_optimization:
                        if hints.load:
                            for rel_name in hints.load:
                                if rel_name in current_mapper.relationships:
                                    extra_rel = current_mapper.relationships[rel_name]
                                    extra_attr = getattr(current_mapper.entity, rel_name)
                                    if extra_rel.uselist:
                                        stmt = stmt.options(selectinload(extra_attr))
                                    else:
                                        stmt = stmt.options(joinedload(extra_attr))
                        if hints.only:
                            cols = [getattr(current_mapper.entity, c) for c in hints.only
                                    if hasattr(current_mapper.entity, c)]
                            if cols:
                                stmt = stmt.options(load_only(*cols))
            return stmt

        def _collect_nested_loaders(parent_loader: Any, selection_set: Any, child_mapper: Any) -> list[Any]:
            """Collect all nested relationship loader chains from a selection set.

            Each sibling relationship becomes a separate loader option branching
            from parent_loader independently, avoiding invalid cross-relationship chaining.
            """
            if selection_set is None:
                return [parent_loader]
            loaders: list[Any] = []
            has_child_rels = False
            for node in selection_set.selections:
                field_name = _get_field_name(node)
                if field_name in child_mapper.relationships:
                    has_child_rels = True
                    rel = child_mapper.relationships[field_name]
                    rel_attr = getattr(child_mapper.entity, field_name)
                    if rel.uselist:
                        child_loader = parent_loader.selectinload(rel_attr)
                    else:
                        child_loader = parent_loader.joinedload(rel_attr)
                    if node.selection_set:
                        nested_mapper = sa_inspect(rel.mapper.class_)
                        nested = _collect_nested_loaders(child_loader, node.selection_set, nested_mapper)
                        loaders.extend(nested)
                    else:
                        loaders.append(child_loader)
            if not has_child_rels:
                loaders.append(parent_loader)
            return loaders

        for field_node in info.field_nodes:
            query = _apply_loads(query, field_node.selection_set, mapper)

        session = self._get_session(info)
        result = session.execute(query).scalars().unique().all()
        return list(result)

    def _type_name_for_model(self, model: type) -> str | None:
        for type_name, m in self._type_registry.items():
            if m is model:
                return type_name
        return None

    # -- Helpers -------------------------------------------------------------

    def _get_session(self, info: Any) -> Any:
        if self._session_getter is not None:
            return self._session_getter(info)
        ctx = info.context
        if isinstance(ctx, dict):
            if "session" in ctx:
                s = ctx["session"]
                return s() if callable(s) else s
        else:
            if hasattr(ctx, "session"):
                s = ctx.session
                return s() if callable(s) else s
            if hasattr(ctx, "get_session"):
                return ctx.get_session()
        raise RuntimeError("SQLAlchemy backend requires a session_getter or info.context.session")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_element_type(ann: Any) -> Any:
    """Extract T from list[T]."""
    import typing
    if typing.get_origin(ann) is list:
        args = typing.get_args(ann)
        if args:
            return args[0]
    return None


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
    eagerly-loaded attribute.  Otherwise issues a fresh query via
    ``with_parent`` so the database handles the filtering/ordering.
    """
    info_type = strawberry.types.Info

    if filter_type and order_type:
        def resolver(self: Any, info: Any, filter: Any = None, order: Any = None) -> Any:
            if filter is None and order is None:
                return getattr(self, fname)
            from sqlalchemy import select
            from sqlalchemy.orm import with_parent
            session = backend._get_session(info)
            stmt = select(rel_model).where(with_parent(self, getattr(parent_model, fname)))
            if filter is not None:
                stmt = backend.apply_filters(stmt, filter, rel_model)
            if order is not None:
                stmt = backend.apply_ordering(stmt, order, rel_model)
            return session.execute(stmt).scalars().all()
        resolver.__annotations__ = {
            "info": info_type,
            "filter": Optional[filter_type],
            "order": Optional[order_type],
        }
    elif filter_type:
        def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            if filter is None:
                return getattr(self, fname)
            from sqlalchemy import select
            from sqlalchemy.orm import with_parent
            session = backend._get_session(info)
            stmt = select(rel_model).where(with_parent(self, getattr(parent_model, fname)))
            stmt = backend.apply_filters(stmt, filter, rel_model)
            return session.execute(stmt).scalars().all()
        resolver.__annotations__ = {
            "info": info_type,
            "filter": Optional[filter_type],
        }
    else:
        def resolver(self: Any, info: Any, order: Any = None) -> Any:
            if order is None:
                return getattr(self, fname)
            from sqlalchemy import select
            from sqlalchemy.orm import with_parent
            session = backend._get_session(info)
            stmt = select(rel_model).where(with_parent(self, getattr(parent_model, fname)))
            stmt = backend.apply_ordering(stmt, order, rel_model)
            return session.execute(stmt).scalars().all()
        resolver.__annotations__ = {
            "info": info_type,
            "order": Optional[order_type],
        }

    return strawberry.field(resolver=resolver)


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


def _introspect_sa_model(
    model: type,
) -> list[tuple[str, type, bool, type | None]]:
    """Return (field_name, python_type, is_relation, related_model) for each
    column and relationship on an SQLAlchemy mapped class."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    result: list[tuple[str, type, bool, type | None]] = []

    for col in mapper.columns:
        col_type_name = type(col.type).__name__.upper()
        py_type = _SA_TYPE_MAP.get(col_type_name, str)

        if hasattr(col.type, "impl"):
            impl_name = type(col.type.impl).__name__.upper()
            py_type = _SA_TYPE_MAP.get(impl_name, py_type)

        result.append((col.key, py_type, False, None))

    for rel in mapper.relationships:
        target_model = rel.mapper.class_
        result.append((rel.key, Any, True, target_model))

    return result


def _input_to_dict(obj: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for f in obj.__class__.__dataclass_fields__:
        val = getattr(obj, f)
        if val is not strawberry.UNSET:
            result[f] = val
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


def _build_sa_filter(filter_input: Any, model: type) -> Any:
    """Recursively translate a filter input object into a SQLAlchemy BooleanClause."""
    from sqlalchemy import and_, or_, not_

    if filter_input is None or filter_input is strawberry.UNSET:
        return None

    fields = filter_input.__class__.__dataclass_fields__

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            return _build_sa_field_clause(val, model)
        elif key == "all":
            sub = [_build_sa_filter(f, model) for f in val]
            sub = [s for s in sub if s is not None]
            return and_(*sub) if sub else None
        elif key == "any":
            sub = [_build_sa_filter(f, model) for f in val]
            sub = [s for s in sub if s is not None]
            return or_(*sub) if sub else None
        elif key == "not_":
            inner = _build_sa_filter(val, model)
            return not_(inner) if inner is not None else None
        elif key == "one_of":
            sub = [_build_sa_filter(f, model) for f in val]
            sub = [s for s in sub if s is not None]
            return or_(*sub) if sub else None

    return None


def _build_sa_field_clause(field_input: Any, model: type) -> Any:
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

        col_clauses = _build_lookup_clauses(column, lookup)
        clauses.extend(col_clauses)

    if not clauses:
        return None
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def _build_lookup_clauses(column: Any, lookup: Any) -> list[Any]:
    """Translate a single lookup object (e.g. StringLookup) into clauses."""
    clauses = []
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            clauses.append(column.is_(None) if val else column.isnot(None))
        elif op_name == "in_list":
            clauses.append(column.in_(val))
        elif op_name == "not_in_list":
            clauses.append(column.notin_(val))
        elif op_name == "range":
            clauses.append(column.between(val.start, val.end))
        elif op_name == "contains":
            clauses.append(column.contains(val))
        elif op_name == "i_contains":
            clauses.append(column.ilike(f"%{val}%"))
        elif op_name == "starts_with":
            clauses.append(column.startswith(val))
        elif op_name == "i_starts_with":
            clauses.append(column.ilike(f"{val}%"))
        elif op_name == "ends_with":
            clauses.append(column.endswith(val))
        elif op_name == "i_ends_with":
            clauses.append(column.ilike(f"%{val}"))
        elif op_name == "regex":
            clauses.append(column.regexp_match(val))
        elif op_name == "i_regex":
            clauses.append(column.regexp_match(val, flags="i"))
        elif op_name in ("exact", "neq", "gt", "gte", "lt", "lte"):
            sa_op = _LOOKUP_TO_SA_OP[op_name]
            clauses.append(getattr(column, sa_op)(val))

    return clauses


# ---------------------------------------------------------------------------
# Ordering translation
# ---------------------------------------------------------------------------

def _build_sa_ordering(order_input: Any, model: type) -> list[Any]:
    """Translate an order input object into a list of SQLAlchemy order_by clauses."""
    from sqlalchemy import asc, desc

    clauses = []
    fields = order_input.__class__.__dataclass_fields__

    for col_name in fields:
        direction = getattr(order_input, col_name)
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
