"""SQLAlchemy backend -- built from scratch using SQLAlchemy introspection."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Callable, Optional

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm._async import run_sync
from strawberry_orm.backends._base import (
    BaseBackend,
    _SENSITIVE_PATTERNS,
    extract_element_type,
    input_to_dict,
)
from strawberry_orm.optimizer import OptimizerExtension


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
        **kwargs: Any,
    ) -> None:
        kwargs["filter_overrides"] = filter_overrides or {}
        super().__init__(**kwargs)
        self._dialect = dialect
        self._session_getter = session_getter
        self._max_filter_depth = max_filter_depth
        self._max_filter_branches = max_filter_branches
        self._enable_regex_filters = enable_regex_filters
        self._max_in_list_size = max_in_list_size

    # -- Introspection -------------------------------------------------------

    def _introspect_model(
        self, model: type
    ) -> list[tuple[str, type, bool, type | None]]:
        """Return (field_name, python_type, is_relation, related_model) for each
        column and relationship on an SQLAlchemy mapped class."""
        return _introspect_sa_model(model)

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")

        def decorator(cls: type) -> Any:
            from sqlalchemy import inspect as sa_inspect

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

            # PEP 563: resolve string annotations via get_type_hints
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
            )

            # SA-specific: relation resolvers for list fields
            for field_name, ann in cls.__annotations__.items():
                if field_name in vars(cls):
                    continue
                el_type = extract_element_type(ann)
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
            if self._exclude_sensitive_fields and not (include and fname in include):
                if _SENSITIVE_PATTERNS.search(fname):
                    continue
            if is_relation:
                continue
            annotations[fname] = Optional[ftype]
            defaults[fname] = strawberry.UNSET

        type_name = name or f"{model.__name__}Input"
        ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
        cls = type(type_name, (), ns)
        return strawberry.input(cls)

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Callable[..., bool] | None = None,
        mode: str = "replace",
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
                mode=mode,
            )

        return self._apply_ref_list_sync(
            session,
            instance,
            field,
            refs,
            info,
            authorize,
            mode,
        )

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        clause = _build_sa_filter(
            filter_input,
            model,
            max_depth=self._max_filter_depth,
            max_branches=self._max_filter_branches,
            enable_regex=self._enable_regex_filters,
            max_in_list_size=self._max_in_list_size,
        )
        if clause is not None:
            query = query.where(clause)
        return query

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        order_list = order_input if isinstance(order_input, list) else [order_input]
        clauses: list[Any] = []
        for entry in order_list:
            clauses.extend(_build_sa_ordering(entry, model))
        if clauses:
            query = query.order_by(*clauses)
        return query

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        from sqlalchemy import select

        stmt = select(model)
        if self._default_query_limit is not None:
            stmt = stmt.limit(self._default_query_limit)
        return stmt

    def is_query_object(self, value: Any) -> bool:
        try:
            from sqlalchemy.sql import Select

            return isinstance(value, Select)
        except ImportError:
            return False

    def materialize_query(self, query: Any, info: Any) -> Any:
        return self._execute_stmt(query, info)

    # -- Optimizer -----------------------------------------------------------

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

        type_name = self._type_name_for_model(parent_entity)
        if type_name:
            hints = self._store.get(type_name, field_name)
            if hints and callable(hints.load):
                stmt = hints.load(stmt)

        return stmt

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
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

        def _get_nested_criteria(
            parent_entity: type,
            field_name: str,
            related_entity: type,
        ) -> Any:
            """Extract WHERE criteria from load callable / nested get_queryset."""
            from sqlalchemy import select as sa_select

            criteria_parts: list[Any] = []

            get_qs = self._type_querysets.get(related_entity)
            if get_qs is not None:
                temp_stmt = get_qs(sa_select(related_entity), info)
                if temp_stmt.whereclause is not None:
                    criteria_parts.append(temp_stmt.whereclause)

            type_name = self._type_name_for_model(parent_entity)
            if type_name and store:
                hints = store.get(type_name, field_name)
                if hints and callable(hints.load):
                    temp_stmt = hints.load(sa_select(related_entity))
                    if temp_stmt.whereclause is not None:
                        criteria_parts.append(temp_stmt.whereclause)

            if not criteria_parts:
                return None

            if len(criteria_parts) == 1:
                return criteria_parts[0]

            from sqlalchemy import and_ as sa_and

            return sa_and(*criteria_parts)

        def _make_loader(rel_attr: Any, uselist: bool, criteria: Any) -> Any:
            """Build a joinedload or selectinload, optionally with criteria."""
            if uselist:
                if criteria is not None:
                    return selectinload(rel_attr.and_(criteria))
                return selectinload(rel_attr)
            else:
                if criteria is not None:
                    return joinedload(rel_attr.and_(criteria))
                return joinedload(rel_attr)

        def _make_chained_loader(
            parent_loader: Any,
            rel_attr: Any,
            uselist: bool,
            criteria: Any,
        ) -> Any:
            """Chain a nested loader onto parent_loader, optionally with criteria."""
            if uselist:
                if criteria is not None:
                    return parent_loader.selectinload(rel_attr.and_(criteria))
                return parent_loader.selectinload(rel_attr)
            else:
                if criteria is not None:
                    return parent_loader.joinedload(rel_attr.and_(criteria))
                return parent_loader.joinedload(rel_attr)

        def _apply_loads(stmt: Any, selection_set: Any, current_mapper: Any) -> Any:
            if selection_set is None:
                return stmt
            for node in selection_set.selections:
                field_name = _get_field_name(node)

                if field_name in current_mapper.relationships:
                    rel = current_mapper.relationships[field_name]
                    rel_attr = getattr(current_mapper.entity, field_name)
                    criteria = _get_nested_criteria(
                        current_mapper.entity,
                        field_name,
                        rel.mapper.class_,
                    )
                    base_loader = _make_loader(rel_attr, rel.uselist, criteria)

                    if node.selection_set:
                        child_mapper = sa_inspect(rel.mapper.class_)
                        nested = _collect_nested_loaders(
                            base_loader, node.selection_set, child_mapper
                        )
                        for nl in nested:
                            stmt = stmt.options(nl)
                    else:
                        stmt = stmt.options(base_loader)

                type_name = self._type_name_for_model(current_mapper.entity)
                if type_name and store:
                    hints = store.get(type_name, field_name)
                    if hints and not hints.disable_optimization:
                        if hints.load and not callable(hints.load):
                            for rel_name in hints.load:
                                if rel_name in current_mapper.relationships:
                                    extra_rel = current_mapper.relationships[rel_name]
                                    extra_attr = getattr(
                                        current_mapper.entity, rel_name
                                    )
                                    if extra_rel.uselist:
                                        stmt = stmt.options(selectinload(extra_attr))
                                    else:
                                        stmt = stmt.options(joinedload(extra_attr))
                        if hints.only:
                            cols = [
                                getattr(current_mapper.entity, c)
                                for c in hints.only
                                if hasattr(current_mapper.entity, c)
                            ]
                            if cols:
                                stmt = stmt.options(load_only(*cols))
            return stmt

        def _collect_nested_loaders(
            parent_loader: Any, selection_set: Any, child_mapper: Any
        ) -> list[Any]:
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
                    criteria = _get_nested_criteria(
                        child_mapper.entity,
                        field_name,
                        rel.mapper.class_,
                    )
                    child_loader = _make_chained_loader(
                        parent_loader,
                        rel_attr,
                        rel.uselist,
                        criteria,
                    )
                    if node.selection_set:
                        nested_mapper = sa_inspect(rel.mapper.class_)
                        nested = _collect_nested_loaders(
                            child_loader, node.selection_set, nested_mapper
                        )
                        loaders.extend(nested)
                    else:
                        loaders.append(child_loader)
            if not has_child_rels:
                loaders.append(parent_loader)
            return loaders

        for field_node in info.field_nodes:
            query = _apply_loads(query, field_node.selection_set, mapper)
        return self._execute_stmt(query, info)

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
        try:
            from sqlalchemy.ext.asyncio import AsyncSession
        except ImportError:
            return False

        return isinstance(session, AsyncSession)

    def _execute_stmt(self, stmt: Any, info: Any) -> Any:
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._execute_stmt_async(session, stmt)

        return self._execute_stmt_sync(session, stmt)

    def _execute_stmt_sync(self, session: Any, stmt: Any) -> list[Any]:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            result = session.execute(stmt)
        except (OperationalError, ProgrammingError):
            raise ValueError("Invalid filter expression") from None

        return list(result.scalars().unique().all())

    async def _execute_stmt_async(self, session: Any, stmt: Any) -> list[Any]:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        try:
            result = await session.execute(stmt)
        except (OperationalError, ProgrammingError):
            raise ValueError("Invalid filter expression") from None

        return list(result.scalars().unique().all())

    def _apply_ref_list_sync(
        self,
        session: Any,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        authorize: Callable[..., bool] | None,
        mode: str,
    ) -> None:
        relationship = getattr(type(instance), field).property
        target_model = relationship.mapper.class_

        new_related: list[Any] = []
        to_remove: list[Any] = []
        for ref in refs:
            ref_id = getattr(ref, "id", strawberry.UNSET)
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_id is not strawberry.UNSET and ref_id is not None:
                if authorize and not authorize("link", target_model, ref_id, info):
                    continue
                obj = session.get(target_model, ref_id)
                if obj is not None:
                    new_related.append(obj)
            elif ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", target_model, None, info):
                    continue
                obj = target_model(**input_to_dict(ref_create))
                session.add(obj)
                new_related.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", target_model, pk, info):
                    continue
                obj = session.get(target_model, pk)
                if obj is not None:
                    for k, v in data.items():
                        setattr(obj, k, v)
                    new_related.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", target_model, ref_delete.id, info
                ):
                    continue
                obj = session.get(target_model, ref_delete.id)
                if obj is not None:
                    to_remove.append(obj)
                    if self._hard_delete_refs:
                        session.delete(obj)

        if mode == "patch":
            existing = list(getattr(instance, field))
            merged = [o for o in existing if o not in to_remove]
            for obj in new_related:
                if obj not in merged:
                    merged.append(obj)
            setattr(instance, field, merged)
        else:
            setattr(instance, field, new_related)

    async def _apply_ref_list_async(
        self,
        session: Any,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Callable[..., bool] | None,
        mode: str,
    ) -> None:
        relationship = getattr(type(instance), field).property
        target_model = relationship.mapper.class_

        new_related: list[Any] = []
        to_remove: list[Any] = []
        for ref in refs:
            ref_id = getattr(ref, "id", strawberry.UNSET)
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_id is not strawberry.UNSET and ref_id is not None:
                if authorize and not authorize("link", target_model, ref_id, info):
                    continue
                obj = await session.get(target_model, ref_id)
                if obj is not None:
                    new_related.append(obj)
            elif ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", target_model, None, info):
                    continue
                obj = target_model(**input_to_dict(ref_create))
                session.add(obj)
                new_related.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", target_model, pk, info):
                    continue
                obj = await session.get(target_model, pk)
                if obj is not None:
                    for k, v in data.items():
                        setattr(obj, k, v)
                    new_related.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", target_model, ref_delete.id, info
                ):
                    continue
                obj = await session.get(target_model, ref_delete.id)
                if obj is not None:
                    to_remove.append(obj)
                    if self._hard_delete_refs:
                        await session.delete(obj)

        if mode == "patch":
            await session.refresh(instance, [field])
            existing = list(getattr(instance, field))
            merged = [o for o in existing if o not in to_remove]
            for obj in new_related:
                if obj not in merged:
                    merged.append(obj)
            setattr(instance, field, merged)
        else:
            setattr(instance, field, new_related)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
                return getattr(self, fname)
            stmt = _build_stmt(self, info)
            if filter is not None:
                stmt = backend.apply_filters(stmt, filter, rel_model)
            if order is not None:
                stmt = backend.apply_ordering(stmt, order, rel_model)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": Optional[filter_type],
            "order": Optional[list[order_type]],
        }
    elif filter_type:

        def resolver(self: Any, info: Any, filter: Any = None) -> Any:
            if filter is None:
                return getattr(self, fname)
            stmt = _build_stmt(self, info)
            stmt = backend.apply_filters(stmt, filter, rel_model)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "filter": Optional[filter_type],
        }
    else:

        def resolver(self: Any, info: Any, order: Any = None) -> Any:
            if order is None:
                return getattr(self, fname)
            stmt = _build_stmt(self, info)
            stmt = backend.apply_ordering(stmt, order, rel_model)
            return _execute(stmt, info)

        resolver.__annotations__ = {
            "info": info_type,
            "order": Optional[list[order_type]],
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


def _get_sa_pk_names(model: type) -> set[str]:
    """Return the set of primary-key column attribute names for an SA model."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    return {col.key for col in mapper.primary_key}


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


def _build_sa_filter(
    filter_input: Any,
    model: type,
    *,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = True,
    max_in_list_size: int = 500,
    _depth: int = 0,
) -> Any:
    """Recursively translate a filter input object into a SQLAlchemy BooleanClause."""
    from sqlalchemy import and_, or_, not_

    if _depth > max_depth:
        raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")

    if filter_input is None or filter_input is strawberry.UNSET:
        return None

    fields = filter_input.__class__.__dataclass_fields__
    recurse_kw = dict(
        max_depth=max_depth,
        max_branches=max_branches,
        enable_regex=enable_regex,
        max_in_list_size=max_in_list_size,
        _depth=_depth + 1,
    )

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            return _build_sa_field_clause(
                val,
                model,
                enable_regex=enable_regex,
                max_in_list_size=max_in_list_size,
            )
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_sa_filter(f, model, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            return and_(*sub) if sub else None
        elif key == "any":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_sa_filter(f, model, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            return or_(*sub) if sub else None
        elif key == "not_":
            inner = _build_sa_filter(val, model, **recurse_kw)
            return not_(inner) if inner is not None else None
        elif key == "one_of":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_sa_filter(f, model, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            return or_(*sub) if sub else None

    return None


def _build_sa_field_clause(
    field_input: Any,
    model: type,
    *,
    enable_regex: bool = True,
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


def _build_lookup_clauses(
    column: Any,
    lookup: Any,
    *,
    enable_regex: bool = True,
    max_in_list_size: int = 500,
) -> list[Any]:
    """Translate a single lookup object (e.g. StringLookup) into clauses."""
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
            if op_name == "in_list":
                clauses.append(column.in_(val))
            else:
                clauses.append(column.notin_(val))
        elif op_name == "range":
            clauses.append(column.between(val.start, val.end))
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
