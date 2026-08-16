"""SQLAlchemy backend -- built from scratch using SQLAlchemy introspection."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm.backends._base import (
    _SENSITIVE_PATTERNS,
    AggregateMeta,
    BaseBackend,
    extract_element_type,
    input_to_dict,
    invoke_custom_callback,
    requested_aggregates,
)
from strawberry_orm.backends.filter_pk_shortcut import (
    build_reference_object_filter_clause,
)
from strawberry_orm.fields import call_scope
from strawberry_orm.filters import is_fk_shortcut_lookup, is_reference_lookup
from strawberry_orm.optimizer import OptimizerExtension
from strawberry_orm.types import DateGroupByInterval


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

    def _get_pk_names(self, model: type) -> set[str]:
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(model)
        return {col.key for col in mapper.primary_key}

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

                @classmethod
                def is_type_of(inner_cls: type, obj: object, info: Any) -> bool:
                    return isinstance(obj, model)

                cls.is_type_of = is_type_of  # type: ignore[method-assign]

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
                    py_type = py_type | None
                col_types[col.key] = py_type

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

    # -- Query application ----------------------------------------------------

    def apply_filters(
        self, query: Any, filter_input: Any, model: type, info: Any = None
    ) -> Any:
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
        order_list = order_input if isinstance(order_input, list) else [order_input]
        clauses: list[Any] = []
        joins: list[Any] = []
        for entry in order_list:
            entry_clauses, entry_joins, query = _build_sa_ordering(
                entry, model, query=query, backend=self
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
        return query

    # -- Grouping / aggregation -----------------------------------------------

    def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: AggregateMeta
    ) -> Any:
        from sqlalchemy import func, select

        requested = requested_aggregates(info, "aggregates") or {}
        if not requested and not aggregate_meta.custom_fields:
            return aggregate_meta.aggregates_type(count=0)

        subq = query.subquery()
        agg_cols: list[Any] = []

        if requested.get("count"):
            agg_cols.append(func.count().label("_count"))
        for func_name, sql_func in [
            ("sum", func.sum),
            ("avg", func.avg),
            ("min", func.min),
            ("max", func.max),
        ]:
            for fname in requested.get(func_name, []):
                col = subq.c.get(fname)
                if col is not None:
                    agg_cols.append(sql_func(col).label(f"_{func_name}_{fname}"))

        for field_name, handler, _rtype in aggregate_meta.custom_fields:
            expr = _invoke_aggregate_handler(handler, subq.c, info=info)
            if expr is not None:
                agg_cols.append(expr.label(f"_custom_{field_name}"))

        if not agg_cols:
            return aggregate_meta.aggregates_type(count=0)

        stmt = select(*agg_cols).select_from(subq)
        session = self._get_session(info)
        if self._is_async_session(session):
            return self._apply_aggregation_async(
                session, stmt, aggregate_meta, requested
            )
        result = session.execute(stmt)
        row = result.one()
        return aggregate_meta.build_aggregates(row, requested)

    async def _apply_aggregation_async(
        self, session: Any, stmt: Any, meta: AggregateMeta, requested: dict
    ) -> Any:
        result = await session.execute(stmt)
        row = result.one()
        return meta.build_aggregates(row, requested)

    def apply_grouping(
        self,
        query: Any,
        group_by_input: Any,
        info: Any,
        aggregate_meta: AggregateMeta,
        *,
        order_input: Any | None = None,
    ) -> list[Any]:
        from sqlalchemy import select

        model = aggregate_meta.model
        group_by_list = (
            group_by_input if isinstance(group_by_input, list) else [group_by_input]
        )

        subq = query.subquery()

        group_cols, group_key_fields = _extract_sa_group_columns(
            group_by_list, model, subq
        )
        aggregate_meta.group_key_fields = group_key_fields

        if not group_cols:
            return []

        requested = requested_aggregates(info, "groups.aggregates") or {}
        agg_cols = _build_sa_agg_cols(
            model,
            requested,
            subq,
            custom_fields=aggregate_meta.custom_fields,
            info=info,
        )

        stmt = select(*group_cols, *agg_cols).select_from(subq).group_by(*group_cols)

        if order_input:
            order_clauses = _extract_overlapping_order(
                order_input, set(group_key_fields), model, subq
            )
            if order_clauses:
                stmt = stmt.order_by(*order_clauses)

        session = self._get_session(info)
        if self._is_async_session(session):
            return self._apply_grouping_async(
                session, stmt, aggregate_meta, requested, group_key_fields
            )

        result = session.execute(stmt)
        rows = result.all()
        return [
            _build_sa_group(row, aggregate_meta, requested, group_key_fields)
            for row in rows
        ]

    async def _apply_grouping_async(
        self, session, stmt, meta, requested, group_key_fields
    ):
        result = await session.execute(stmt)
        rows = result.all()
        return [_build_sa_group(row, meta, requested, group_key_fields) for row in rows]

    def scope_query_to_group(self, query: Any, group_key: Any) -> Any:
        key_fields = group_key.__class__.__dataclass_fields__
        for fname in key_fields:
            val = getattr(group_key, fname, None)
            if val is not None:
                col = getattr(query.column_descriptions[0]["entity"], fname, None)
                if col is not None:
                    query = query.where(col == val)
        return query

    def batch_group_items(
        self,
        query: Any,
        group_key_fields: list[str],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
    ) -> dict[tuple, list[Any]]:
        from collections import defaultdict

        from sqlalchemy import func, select

        key_cols = [getattr(model, k) for k in group_key_fields]
        if order_input:
            order_clauses = _build_sa_order_from_input(order_input, model)
        else:
            pk_col = _get_sa_pk_column(model)
            order_clauses = [pk_col]

        rn = (
            func.row_number()
            .over(
                partition_by=key_cols,
                order_by=order_clauses,
            )
            .label("_rn")
        )

        subq = query.add_columns(rn).subquery()
        ranked_stmt = select(model).from_statement(
            select(subq).where(subq.c._rn <= per_group_limit)
        )

        session = self._get_session(info)
        if self._is_async_session(session):
            return self._batch_group_items_async(
                session, ranked_stmt, group_key_fields, model
            )

        result = session.execute(ranked_stmt)
        rows = list(result.scalars().unique().all())

        items_by_key: dict[tuple, list[Any]] = defaultdict(list)
        for row in rows:
            key = tuple(
                str(getattr(row, k)) if getattr(row, k) is not None else None
                for k in group_key_fields
            )
            items_by_key[key].append(row)
        return dict(items_by_key)

    async def _batch_group_items_async(self, session, stmt, group_key_fields, model):
        from collections import defaultdict

        result = await session.execute(stmt)
        rows = list(result.scalars().unique().all())

        items_by_key: dict[tuple, list[Any]] = defaultdict(list)
        for row in rows:
            key = tuple(
                str(getattr(row, k)) if getattr(row, k) is not None else None
                for k in group_key_fields
            )
            items_by_key[key].append(row)
        return dict(items_by_key)

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        from sqlalchemy import select

        stmt = select(model)
        if self._default_query_limit is not None:
            stmt = stmt.limit(self._default_query_limit)
        return stmt

    def is_query_object(self, value: Any) -> bool:
        from sqlalchemy.sql import Select

        return isinstance(value, Select)

    def is_model_instance(self, value: Any) -> bool:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.orm.state import InstanceState

        if isinstance(value, type):
            return False
        return isinstance(sa_inspect(value, raiseerr=False), InstanceState)

    def relation_names(self, model: type) -> set[str]:
        from sqlalchemy import inspect as sa_inspect

        return set(sa_inspect(model).relationships.keys())

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

    def count_query(self, query: Any, info: Any) -> int:
        from sqlalchemy import func, select

        subq = query.subquery()
        stmt = select(func.count()).select_from(subq)
        return self._get_session(info).execute(stmt).scalar_one()

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
            from strawberry_orm.batching import page_attr

            page = getattr(self, page_attr(field_name), None)
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

        subq = query.subquery()
        key_col = subq.c[key_field]
        stmt = select(key_col, func.count()).select_from(subq).group_by(key_col)
        # Only reached once the windowed page came back synchronously.
        return dict(self._get_session(info).execute(stmt).all())

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

        type_name = self._type_name_for_model(parent_entity)
        if type_name:
            hints = self._store.get(type_name, field_name)
            if hints and hints.scope is not None:
                stmt = call_scope(hints.scope, stmt, info)

        return stmt

    def _apply_relation_loads(
        self, store: Any, query: Any, entity: type, info: Any
    ) -> Any:
        """Attach eager-load options for the current selection to *query*.

        Shared by query optimization and by loading relations onto instances
        the caller already holds, so both apply the same row scoping.
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.orm import joinedload, selectinload

        from strawberry_orm.optimizer.selections import (
            field_nodes_from_info,
            fragments_from_info,
            iter_field_nodes,
        )

        fragments = fragments_from_info(info)
        mapper = sa_inspect(entity)

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
            """Extract WHERE criteria from a scope callable / nested scope_rows."""
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
                if hints and hints.scope is not None:
                    temp_stmt = call_scope(hints.scope, sa_select(related_entity), info)
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

        def _hint_relations(current_mapper: Any, field_name: str) -> list[str]:
            """Relation names declared on ``using=`` for *field_name*.

            Keyed on the field name alone, so computed fields that are not mapped
            attributes can still declare the relations their resolver reads.
            """
            type_name = self._type_name_for_model(current_mapper.entity)
            hints = store.get(type_name, field_name) if type_name and store else None
            if not hints or hints.disable_optimization or not hints.using:
                return []
            return [
                name for name in hints.using if name in current_mapper.relationships
            ]

        def _apply_loads(stmt: Any, selection_set: Any, current_mapper: Any) -> Any:
            if selection_set is None:
                return stmt
            for node in iter_field_nodes(selection_set, fragments):
                field_name = _get_field_name(node)

                # A field that answers for itself will ignore whatever this
                # loads, so do not pay for it.
                if self.resolves_itself(
                    self._type_name_for_model(current_mapper.entity), field_name
                ):
                    continue

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

                for rel_name in _hint_relations(current_mapper, field_name):
                    extra_rel = current_mapper.relationships[rel_name]
                    extra_attr = getattr(current_mapper.entity, rel_name)
                    if extra_rel.uselist:
                        stmt = stmt.options(selectinload(extra_attr))
                    else:
                        stmt = stmt.options(joinedload(extra_attr))

            return stmt

        def _collect_nested_loaders(
            parent_loader: Any, selection_set: Any, child_mapper: Any
        ) -> list[Any]:
            """Collect all nested relationship loader chains from a selection set.

            Each sibling relationship becomes a separate loader option branching
            from parent_loader independently, avoiding invalid cross-relationship chaining.
            """
            if selection_set is None:
                return [parent_loader]  # pragma: no cover
            loaders: list[Any] = []
            has_child_rels = False
            for node in iter_field_nodes(selection_set, fragments):
                field_name = _get_field_name(node)

                for rel_name in _hint_relations(child_mapper, field_name):
                    has_child_rels = True
                    hint_rel = child_mapper.relationships[rel_name]
                    loaders.append(
                        _make_chained_loader(
                            parent_loader,
                            getattr(child_mapper.entity, rel_name),
                            hint_rel.uselist,
                            None,
                        )
                    )

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

        for field_node in field_nodes_from_info(info):
            query = _apply_loads(query, field_node.selection_set, mapper)
        return query

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        try:
            entity = query.column_descriptions[0]["entity"]
        except (AttributeError, IndexError, KeyError):
            return query

        get_qs = self._type_querysets.get(entity)
        if get_qs is not None:
            query = get_qs(query, info)

        return self._execute_stmt(
            self._apply_relation_loads(store, query, entity, info), info
        )

    def load_relations(self, store: Any, instances: list[Any], info: Any) -> list[Any]:
        """Eager-load the selected relations onto instances already in memory.

        Re-selecting the same primary keys populates the relations through the
        session's identity map, which hands back the very objects the caller
        passed in. Scalars already loaded on them are left alone, so values
        fresher than the database survive.

        Returns the rows it actually loaded onto, which is empty when the
        selection named no relations.
        """
        plans = self._relation_load_statements(store, instances, info)
        if not plans:
            return []

        session = self._get_session(info)
        loaded = [row for _stmt, rows in plans for row in rows]
        if self._is_async_session(session):

            async def _load_async() -> list[Any]:
                for stmt, _rows in plans:
                    await self._execute_stmt_async(session, stmt)
                return loaded

            return _load_async()

        for stmt, _rows in plans:
            self._execute_stmt_sync(session, stmt)
        return loaded

    def _relation_load_statements(
        self, store: Any, instances: list[Any], info: Any
    ) -> list[tuple[Any, list[Any]]]:
        """Pair each model's re-select with the rows it will load onto.

        A statement is only worth running when the selection asked for
        relations; ``options()`` returns a new statement, so one that came back
        unchanged means re-selecting the rows would buy nothing.
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import select as sa_select

        by_model: dict[type, list[Any]] = {}
        for instance in instances:
            by_model.setdefault(type(instance), []).append(instance)

        plans: list[tuple[Any, list[Any]]] = []
        for model, rows in by_model.items():
            mapper = sa_inspect(model)
            pk_column = mapper.primary_key[0]
            pk_name = mapper.get_property_by_column(pk_column).key
            stmt = sa_select(model).where(
                pk_column.in_([getattr(row, pk_name) for row in rows])
            )
            with_loads = self._apply_relation_loads(store, stmt, model, info)
            if with_loads is not stmt:
                plans.append((with_loads, rows))
        return plans

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
        from sqlalchemy.ext.asyncio import AsyncSession

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
    ) -> None:
        from strawberry_orm.repo import _check_auth

        relationship = getattr(type(instance), field).property
        target_model = relationship.mapper.class_
        repo = self.get_repo(target_model) if not authorize else None

        to_add: list[Any] = []
        to_remove: list[Any] = []
        to_delete: list[Any] = []

        for ref in refs:
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", target_model, None, info):
                    continue
                data = input_to_dict(ref_create)
                if repo is not None:
                    data = repo.on_before_create(data, info)
                _check_auth(repo, "can_create", data, info)
                if repo is not None:
                    obj = repo._create(target_model, data, info)
                else:
                    obj = target_model(**data)
                    session.add(obj)
                if repo is not None:
                    repo.on_after_create(obj, info)
                to_add.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", target_model, pk, info):
                    continue
                if repo is not None:
                    obj = repo._get(target_model, pk, info)
                else:
                    obj = session.get(target_model, pk)
                if obj is not None:
                    if repo is not None:
                        data = repo.on_before_update(obj, data, info)
                    _check_auth(repo, "can_update", obj, data, info)
                    _check_auth(repo, "can_link", instance, field, obj, info)
                    for k, v in data.items():
                        setattr(obj, k, v)
                    if repo is not None:
                        repo._save(obj, info)
                        repo.on_after_update(obj, info)
                    to_add.append(obj)
            elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                if authorize and not authorize(
                    "unlink", target_model, ref_unlink.id, info
                ):
                    continue
                if repo is not None:
                    obj = repo._get(target_model, ref_unlink.id, info)
                else:
                    obj = session.get(target_model, ref_unlink.id)
                if obj is not None:
                    _check_auth(repo, "can_unlink", instance, field, obj, info)
                    to_remove.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", target_model, ref_delete.id, info
                ):
                    continue
                if repo is not None:
                    obj = repo._get(target_model, ref_delete.id, info)
                else:
                    obj = session.get(target_model, ref_delete.id)
                if obj is not None:
                    _check_auth(repo, "can_delete", obj, info)
                    if repo is not None:
                        repo.on_before_delete(obj, info)
                    to_delete.append(obj)

        existing = list(getattr(instance, field))
        merged = [o for o in existing if o not in to_remove and o not in to_delete]
        for obj in to_add:
            if obj not in merged:
                merged.append(obj)
        setattr(instance, field, merged)
        for obj in to_delete:
            session.delete(obj)

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
        from strawberry_orm.repo import _check_auth

        relationship = getattr(type(instance), field).property
        target_model = relationship.mapper.class_
        repo = self.get_repo(target_model) if not authorize else None

        to_add: list[Any] = []
        to_remove: list[Any] = []
        to_delete: list[Any] = []

        for ref in refs:
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", target_model, None, info):
                    continue
                data = input_to_dict(ref_create)
                if repo is not None:
                    data = repo.on_before_create(data, info)
                _check_auth(repo, "can_create", data, info)
                if repo is not None:
                    obj = await repo._create_async(target_model, data, info)
                else:
                    obj = target_model(**data)
                    session.add(obj)
                if repo is not None:
                    repo.on_after_create(obj, info)
                to_add.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", target_model, pk, info):
                    continue
                if repo is not None:
                    obj = await repo._get_async(target_model, pk, info)
                else:
                    obj = await session.get(target_model, pk)
                if obj is not None:
                    if repo is not None:
                        data = repo.on_before_update(obj, data, info)
                    _check_auth(repo, "can_update", obj, data, info)
                    _check_auth(repo, "can_link", instance, field, obj, info)
                    for k, v in data.items():
                        setattr(obj, k, v)
                    if repo is not None:
                        await repo._save_async(obj, info)
                        repo.on_after_update(obj, info)
                    to_add.append(obj)
            elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                if authorize and not authorize(
                    "unlink", target_model, ref_unlink.id, info
                ):
                    continue
                if repo is not None:
                    obj = await repo._get_async(target_model, ref_unlink.id, info)
                else:
                    obj = await session.get(target_model, ref_unlink.id)
                if obj is not None:
                    _check_auth(repo, "can_unlink", instance, field, obj, info)
                    to_remove.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", target_model, ref_delete.id, info
                ):
                    continue
                if repo is not None:
                    obj = await repo._get_async(target_model, ref_delete.id, info)
                else:
                    obj = await session.get(target_model, ref_delete.id)
                if obj is not None:
                    _check_auth(repo, "can_delete", obj, info)
                    if repo is not None:
                        repo.on_before_delete(obj, info)
                    to_delete.append(obj)

        await session.refresh(instance, [field])
        existing = list(getattr(instance, field))
        merged = [o for o in existing if o not in to_remove and o not in to_delete]
        for obj in to_add:
            if obj not in merged:
                merged.append(obj)
        setattr(instance, field, merged)
        for obj in to_delete:
            await session.delete(obj)


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

    An already-loaded collection was scoped on the way in; re-querying it would
    both waste a round trip and discard the optimizer's work.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import with_parent

    from strawberry_orm.lazy_resolution import _sqlalchemy_relation_prefetched

    if _sqlalchemy_relation_prefetched(instance, fname):
        return getattr(instance, fname)

    restrict = backend.relation_scope(parent_model, fname, info)
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
    return strawberry.field(resolver=resolver)


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


def _get_sa_pk_column(model: type) -> Any:
    """Return the primary-key column attribute for scoped queries."""
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    pk_col = mapper.primary_key[0]
    return getattr(model, pk_col.key)


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

        fk_target = None
        if col.foreign_keys:
            fk = next(iter(col.foreign_keys))
            target_table = fk.column.table if fk.column is not None else None
            if target_table is not None:
                for rel_mapper in mapper.registry.mappers:
                    if rel_mapper.local_table is target_table:
                        fk_target = rel_mapper.class_
                        break

        result.append((col.key, py_type, False, fk_target))

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
    backend: Any, model: type, relation: str, info: Any
) -> Any | None:
    """Criteria limiting *relation* to the rows its type's scope allows.

    The whole scoped statement is embedded as a subquery over primary keys
    rather than lifting its WHERE clause, so joins, limits and anything else
    ``scope_rows`` adds are carried across intact. Any ``scope=`` on this
    edge is applied too, so a filter cannot reach rows the read path hides.
    """
    if backend is None:
        return None
    get_qs = backend.relation_scope(model, relation, info)
    if get_qs is None:
        return None

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select as sa_select

    related = backend._relation_target_model(model, relation)
    scoped = get_qs(sa_select(related), info)
    pk_col = sa_inspect(related).primary_key[0]
    return pk_col.in_(scoped.with_only_columns(pk_col).scalar_subquery())


def _build_sa_filter(
    filter_input: Any,
    model: type,
    *,
    query: Any = None,
    info: Any = None,
    backend: Any = None,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = True,
    max_in_list_size: int = 500,
    _depth: int = 0,
) -> tuple[Any, Any]:
    """Return ``(clause | None, query)``."""
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
                scope_criteria = _sa_relation_scope_criteria(
                    backend, model, rel_name, info
                )
                is_null_val = getattr(nested_filter, "is_null", strawberry.UNSET)
                if is_null_val is not strawberry.UNSET and is_null_val is not None:
                    if relationship_prop.property.uselist:
                        raise ValueError(
                            f"is_null is not supported for many-relation "
                            f"'{rel_name}'. Use a custom @filter_field for M2M "
                            "presence."
                        )
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
                if not relationship_prop.property.uselist:
                    local_col = list(relationship_prop.property.local_columns)[0]
                    fk_clause = build_reference_object_filter_clause(
                        nested_filter,
                        build_field_clause=_build_sa_reference_field_clause,
                        custom_filter_keys=frozenset(custom_filters.keys()),
                        max_branches=max_branches,
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
                    f, model, **{**recurse_kw, "query": query}
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
            query = invoke_custom_callback(
                custom_filters[key],
                filter_input,
                query=query,
                value=val,
                info=info,
            )
            return None, query

    return None, query


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


def _coerce_reference_value(val: Any) -> Any:
    if isinstance(val, list):
        return [_coerce_reference_value(item) for item in val]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val
    text = str(val)
    try:
        return int(text)
    except ValueError:
        return text


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

        val = _coerce_reference_value(val)

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
        elif op_name == "neq":
            clauses.append(column != val)
        elif op_name == "exact":
            clauses.append(column == val)

    return clauses


def _build_sa_reference_field_clause(
    field_input: Any,
    *,
    local_col: Any,
    enable_regex: bool = True,
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
    enable_regex: bool = True,
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


def _resolve_sa_column(col_name: str, model: type, subq: Any = None) -> Any:
    """Get a column from the subquery (if given) or the model."""
    if subq is not None:
        return subq.c.get(col_name)
    return getattr(model, col_name, None)


def _extract_sa_group_columns(
    group_by_list: list[Any], model: type, subq: Any = None
) -> tuple[list[Any], list[str]]:
    """Extract SQLAlchemy column expressions and field names from group-by input."""
    from sqlalchemy import func

    cols: list[Any] = []
    key_fields: list[str] = []
    seen: set[str] = set()

    for entry in group_by_list:
        field_val = getattr(entry, "field", None)
        if field_val is None or field_val is strawberry.UNSET:
            continue
        entry_fields = field_val.__class__.__dataclass_fields__
        for col_name in entry_fields:
            val = getattr(field_val, col_name)
            if val is strawberry.UNSET or val is None:
                continue
            if col_name in seen:
                continue
            seen.add(col_name)

            column = _resolve_sa_column(col_name, model, subq)
            if column is None:
                continue

            if hasattr(val, "interval"):
                interval = val.interval
                if interval == DateGroupByInterval.DAY:
                    expr = func.date(column)
                elif interval == DateGroupByInterval.WEEK:
                    expr = func.strftime("%Y-%W", column)
                elif interval == DateGroupByInterval.MONTH:
                    expr = func.strftime("%Y-%m", column)
                elif interval == DateGroupByInterval.QUARTER:
                    expr = func.strftime(
                        "%Y-Q",
                        column,
                    )
                elif interval == DateGroupByInterval.YEAR:
                    expr = func.strftime("%Y", column)
                else:
                    expr = column
                cols.append(expr.label(col_name))
            else:
                cols.append(column.label(col_name))
            key_fields.append(col_name)

    return cols, key_fields


def _build_sa_agg_cols(
    model: type,
    requested: dict[str, Any],
    subq: Any = None,
    *,
    custom_fields: list[tuple[str, Any, type]] | None = None,
    info: Any = None,
) -> list[Any]:
    """Build aggregate column expressions based on requested aggregates."""
    from sqlalchemy import func

    agg_cols: list[Any] = []
    if requested.get("count"):
        agg_cols.append(func.count().label("_count"))
    for func_name, sql_func in [
        ("sum", func.sum),
        ("avg", func.avg),
        ("min", func.min),
        ("max", func.max),
    ]:
        for fname in requested.get(func_name, []):
            col = _resolve_sa_column(fname, model, subq)
            if col is not None:
                agg_cols.append(sql_func(col).label(f"_{func_name}_{fname}"))
    if custom_fields:
        columns = subq.c if subq is not None else None
        if columns is not None:
            for field_name, handler, _rtype in custom_fields:
                expr = _invoke_aggregate_handler(handler, columns, info=info)
                if expr is not None:
                    agg_cols.append(expr.label(f"_custom_{field_name}"))
    return agg_cols


def _build_sa_group(
    row: Any,
    meta: Any,
    requested: dict[str, Any],
    group_key_fields: list[str],
) -> Any:
    """Build a group instance from a SQL row."""
    key = meta.build_group_key(row, group_key_fields)
    aggregates = meta.build_aggregates(row, requested)

    group_cls = type(
        "_Group",
        (),
        {
            "key": key,
            "aggregates": aggregates,
            "edge_indices": [],
            "_items_nodes": None,
            "_orm_base_query": None,
            "_orm_backend": None,
            "_orm_model": None,
        },
    )
    return group_cls()


def _extract_overlapping_order(
    order_input: Any,
    group_field_names: set[str],
    model: type,
    subq: Any = None,
) -> list[Any]:
    """Extract ORDER BY clauses for group fields that overlap with root order."""
    from sqlalchemy import asc, desc

    order_list = order_input if isinstance(order_input, list) else [order_input]
    clauses: list[Any] = []

    for entry in order_list:
        field_val = getattr(entry, "field", None)
        if field_val is None or field_val is strawberry.UNSET:
            continue
        entry_fields = field_val.__class__.__dataclass_fields__
        for col_name in entry_fields:
            direction = getattr(field_val, col_name)
            if direction is strawberry.UNSET or direction is None:
                continue
            if col_name not in group_field_names:
                continue
            column = _resolve_sa_column(col_name, model, subq)
            if column is None:
                continue
            dir_value = (
                direction.value if hasattr(direction, "value") else str(direction)
            )
            if dir_value.startswith("ASC"):
                clauses.append(asc(column))
            else:
                clauses.append(desc(column))

    return clauses


def _build_sa_order_from_input(order_input: Any, model: type) -> list[Any]:
    """Convert an order input into SQLAlchemy ORDER BY clauses."""
    order_list = order_input if isinstance(order_input, list) else [order_input]
    clauses: list[Any] = []
    for entry in order_list:
        clauses.extend(_build_sa_order_field_clauses(entry, model))
    if not clauses:
        pk_col = _get_sa_pk_column(model)
        clauses.append(pk_col)
    return clauses


def _build_sa_order_field_clauses(entry: Any, model: type) -> list[Any]:
    """Extract clauses from a single order entry."""
    from sqlalchemy import asc, desc

    clauses: list[Any] = []
    field_val = getattr(entry, "field", None)
    if field_val is None or field_val is strawberry.UNSET:
        return clauses
    entry_fields = field_val.__class__.__dataclass_fields__
    for col_name in entry_fields:
        direction = getattr(field_val, col_name)
        if direction is strawberry.UNSET or direction is None:
            continue
        column = getattr(model, col_name, None)
        if column is None:
            continue
        dir_value = direction.value if hasattr(direction, "value") else str(direction)
        if dir_value.startswith("ASC"):
            clauses.append(asc(column))
        else:
            clauses.append(desc(column))
    return clauses


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
