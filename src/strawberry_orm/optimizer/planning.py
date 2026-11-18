"""Planning the relations a selection reads, at a cost bounded by the document.

Every backend plans the relations below a field's rows the same way: walk the
selection, and load each field that is a relation, and each relation a
field's ``using=`` names, scoped as the type its rows are read as says. A
backend's plan is a tree, as loader options and prefetch paths are, but the
document is not: a fragment spread under two relations is one part of the
document reached two ways, and one spreading the next under both reaches the
last level as many ways as the document is deep, doubled.

So each distinct part - the selections, the model, the types its rows are
read as and the ``using=`` paths passing through - is analysed once per
operation and shared wherever it appears, and the tree is sized before it is
built. Planning an operation may do work, and plan loads, only up to a
budget that grows with the fields the document writes out; past it the
operation is refused before anything is read. Loading less instead would
leave a ``using=`` resolver reading its relation unscoped.

Under an interface, a union or Relay's ``node``, the rows may be read as any
of several types over their model. Each of those types is planned: its own
``using=`` paths, and its own ``scope=`` on each relation it selects. A
relation two of them load at one level is loaded once, with only the rows
every one of their scopes allows; rows cast to one type are planned as that
type alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from graphql import is_abstract_type
from graphql.language.ast import FieldNode, InlineFragmentNode, VariableNode

from strawberry_orm.backends._base import hint_hops
from strawberry_orm.fields import every_scope
from strawberry_orm.lazy_resolution import current_execution
from strawberry_orm.optimizer.selections import (
    _relay_passthrough_field,
    _unwrap_named_type,
    field_nodes_from_info,
    graphql_resolve_info,
    reads_rows_of,
)

#: The loads, and the units of work, an operation may plan per field its
#: document writes out (each fragment's once), on top of ``PLAN_FLOOR``.
PLAN_PER_FIELD = 8
#: The loads, and the units of work, any operation may plan.
PLAN_FLOOR = 200

#: How many wrapper fields deep the type of a field's rows is searched for.
_WRAPPER_DEPTH = 4


class PlanTooLarge(ValueError):
    """An operation's relation loads cost more than its document allows."""


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _document_fields(info: Any) -> int:
    """Fields the document writes out, each fragment's once."""
    raw = graphql_resolve_info(info)
    operation = getattr(raw, "operation", None)
    roots = (
        [operation.selection_set]
        if operation is not None
        else [node.selection_set for node in field_nodes_from_info(info)]
    )
    fragments = getattr(raw, "fragments", None) or {}
    roots += [fragment.selection_set for fragment in fragments.values()]
    found = 0
    pending = [selection_set for selection_set in roots if selection_set is not None]
    while pending:
        for node in pending.pop().selections:
            if isinstance(node, FieldNode):
                found += 1
            if getattr(node, "selection_set", None) is not None:
                pending.append(node.selection_set)
    return found


class _Operation:
    """What planning has done in one operation, and what it may still do."""

    __slots__ = (
        "analyses",
        "budget",
        "every",
        "fields",
        "planned",
        "selections",
        "sizes",
        "spent",
    )

    def __init__(self, info: Any) -> None:
        self.fields = _document_fields(info)
        self.budget = PLAN_FLOOR + PLAN_PER_FIELD * self.fields
        self.spent = 0
        self.analyses: dict[tuple, dict[str, _Load]] = {}
        self.sizes: dict[tuple, int] = {}
        self.planned: set[tuple] = set()
        self.every: dict[tuple[int, ...], Callable[..., Any]] = {}
        #: The selection sets each key names by identity. graphql-core hashes
        #: and compares its nodes by their whole subtree, so keys hold ids.
        self.selections: dict[tuple[int, ...], tuple[Any, ...]] = {}

    def charge(self, units: int) -> None:
        self.spent += units
        if self.spent > self.budget:
            raise PlanTooLarge(
                f"This operation plans more relation loads than its size allows: "
                f"at most {self.budget} for the {self.fields} fields it selects. "
                f"A fragment spread under several relations is loaded under each "
                f"of them, and again under each of those; spread it under fewer, "
                f"or split the operation."
            )


def _operation(info: Any) -> _Operation:
    """The planning state of the operation *info* belongs to.

    Outside an execution there is none to share, so each plan starts afresh
    and is held to the budget on its own.
    """
    execution = current_execution()
    if execution is None:
        return _Operation(info)
    if execution.plans is None:
        execution.plans = _Operation(info)
    return execution.plans


class Relation:
    """A relation of a model that a backend can load."""

    __slots__ = ("detail", "name", "related")

    def __init__(self, name: str, related: type, detail: Any = None) -> None:
        self.name = name
        self.related = related
        #: Whatever the backend needs to build the load.
        self.detail = detail


class _Load:
    """One relation a level loads, and what loads below it."""

    __slots__ = ("below", "owner", "relation", "scopes", "selections", "types", "view")

    def __init__(self, relation: Relation, owner: str | None, view: str | None):
        self.relation = relation
        self.owner = owner
        self.view = view
        self.scopes: list[Any] = []
        self.selections: list[Any] = []
        self.types: list[str | None] = []
        self.below: set[tuple[tuple[str | None, ...], tuple[str, ...]]] = set()


def cast_type_name(row: Any) -> str | None:
    """The name of the type *row* is ``strawberry.cast`` to, if it is cast."""
    from strawberry.types.cast import get_strawberry_type_cast

    cast = get_strawberry_type_cast(row)
    definition = getattr(cast, "__strawberry_definition__", None)
    return getattr(definition, "name", None)


class Planner:
    """Plans the relations one field's selection reads below a model's rows.

    A backend subclasses it to say what a relation of a model is and to build
    its loads; the walk, the types and the budget are shared.
    """

    def __init__(self, backend: Any, store: Any, info: Any) -> None:
        raw = graphql_resolve_info(info)
        self.backend = backend
        self.store = store
        self.info = info
        self.schema = getattr(raw, "schema", None)
        self.fragments = getattr(raw, "fragments", None) or {}
        variables = getattr(raw, "variable_values", None) or {}
        # graphql-core 3.3 holds the coerced values apart from their sources.
        self.variables: dict[str, Any] = getattr(variables, "coerced", variables)
        self.operation = _operation(info)

    # -- What the backend says ------------------------------------------------

    def relation(self, model: type, name: str) -> Relation | None:
        """*model*'s relation *name*, or ``None`` when it has none of that name."""
        raise NotImplementedError

    def selected(
        self, model: type, type_name: str | None, field_name: str
    ) -> tuple[str, Relation, str | None] | None:
        """The load a selected field makes: its key, relation and view, or ``None``."""
        relation = self.relation(model, field_name)
        return None if relation is None else (field_name, relation, None)

    def skips(self, type_name: str | None, field_name: str) -> bool:
        """Whether *field_name* answers without what the plan would load for it."""
        return self.backend.resolves_itself(type_name, field_name)

    def build(self, model: type, key: str, load: _Load, scope: Any, owner: str) -> Any:
        """The backend's load of *load*'s relation, with an empty ``children``."""
        raise NotImplementedError

    # -- Planning -------------------------------------------------------------

    def hints(self, type_name: str | None, field_name: str) -> Any:
        if type_name is None or not self.store:
            return None
        return self.store.get(type_name, field_name)

    def edge_scope(self, type_name: str | None, field_name: str) -> Any:
        hints = self.hints(type_name, field_name)
        return hints.scope if hints else None

    def plan(self, model: type, cast: str | None = None) -> dict[str, Any]:
        """The loads the field's selection needs below rows of *model*.

        *cast* is the type the rows are cast to, which they are read as when
        the field's type allows it.
        """
        types = self.root_types(model)
        if cast is not None and cast in types:
            types = (cast,)
        selections = tuple(
            node.selection_set
            for node in field_nodes_from_info(self.info)
            if node.selection_set is not None
        )
        key = self._key(selections, model, types, frozenset())
        size = self._size(key)
        if key not in self.operation.planned:
            self.operation.charge(size)
            self.operation.planned.add(key)
        return self._expand(key)

    def root_types(self, model: type) -> tuple[str | None, ...]:
        """The types the field's rows of *model* may be read as."""
        return_type = getattr(graphql_resolve_info(self.info), "return_type", None)
        return self._reading(return_type, model) or self._every(model)

    def _every(self, model: type) -> tuple[str | None, ...]:
        """Every type over *model*, for rows whose type the schema does not show."""
        return tuple(self.backend._type_names_of(model)) or (None,)

    def _reading(self, graphql_type: Any, model: type) -> tuple[str, ...]:
        """The types rows of *model* returned as *graphql_type* may be read as.

        A wrapper - a connection's ``edges { node }``, a payload's ``data`` -
        is searched breadth-first for them. Under an interface or a union,
        a row returned as it is reads as the type over its nearest class,
        so each class the rows may be of names its nearest types.
        """
        registry = self.backend._type_registry
        frontier = [_unwrap_named_type(graphql_type)]
        seen: set[Any] = set()
        for _ in range(_WRAPPER_DEPTH):
            exact: list[str] = []
            readers: list[str] = []
            wrapped: list[Any] = []
            for named in frontier:
                if named is None or named in seen:
                    continue
                seen.add(named)
                name = getattr(named, "name", None)
                if name in registry:
                    registered = registry[name]
                    if registered is model:
                        exact.append(name)
                    elif reads_rows_of(registered, model):
                        readers.append(name)
                elif is_abstract_type(named):
                    readers += self._possible(named, model)
                else:
                    fields = getattr(named, "fields", None) or {}
                    wrapped += [_unwrap_named_type(f.type) for f in fields.values()]
            found = exact or readers
            if found:
                return tuple(dict.fromkeys(found))
            frontier = wrapped
        return ()

    def _possible(self, abstract: Any, model: type) -> list[str]:
        """The types of *abstract* rows of *model*, or of a subclass, read as."""
        registry = self.backend._type_registry
        candidates = [
            (graphql_type.name, registry[graphql_type.name])
            for graphql_type in self.schema.get_possible_types(abstract)
            if graphql_type.name in registry
        ]
        classes = [model] + [
            registered
            for _name, registered in candidates
            if registered is not model and reads_rows_of(model, registered)
        ]
        found: list[str] = []
        for cls in classes:
            mro = getattr(cls, "__mro__", (cls,))
            ranked = [
                (mro.index(registered), name)
                for name, registered in candidates
                if registered in mro
            ]
            nearest = min((rank for rank, _name in ranked), default=None)
            found += [name for rank, name in ranked if rank == nearest]
        return found

    def _child_types(
        self, type_name: str | None, field_name: str, related: type
    ) -> tuple[str | None, ...]:
        """The types *field_name* of *type_name* reads rows of *related* as."""
        type_map = getattr(self.schema, "type_map", None)
        if type_map is not None and type_name is not None:
            fields = getattr(type_map.get(type_name), "fields", None) or {}
            field = fields.get(field_name) or fields.get(_camel(field_name))
            if field is not None:
                found = self._reading(field.type, related)
                if found:
                    return found
        return self._every(related)

    def _key(
        self,
        selections: tuple[Any, ...],
        model: type,
        types: Iterable[str | None],
        below: frozenset,
    ) -> tuple:
        types = tuple(dict.fromkeys(types)) if selections else ()
        ids = tuple(id(selection) for selection in selections)
        self.operation.selections.setdefault(ids, selections)
        return (id(self.backend), ids, model, types, below)

    def _size(self, key: tuple) -> int:
        """The loads *key*'s tree holds, analysing what has not been yet."""
        operation = self.operation
        size = operation.sizes.get(key)
        if size is not None:
            return size
        # Valid documents cannot loop; one that does is planned no further.
        operation.sizes[key] = operation.budget + 1
        total = 0
        for load in self._analysis(key).values():
            total += 1 + self._size(self._below(load))
            if total > operation.budget:
                break
        operation.sizes[key] = min(total, operation.budget + 1)
        return operation.sizes[key]

    def _below(self, load: _Load) -> tuple:
        return self._key(
            tuple(load.selections),
            load.relation.related,
            load.types or self._every(load.relation.related),
            frozenset(load.below),
        )

    def _expand(self, key: tuple) -> dict[str, Any]:
        level: dict[str, Any] = {}
        model = key[2]
        for name, load in self._analysis(key).items():
            node = self.build(
                model,
                name,
                load,
                self._combined(load.scopes),
                load.owner or model.__name__,
            )
            node.children.update(self._expand(self._below(load)))
            level[name] = node
        return level

    def _combined(self, scopes: list[Any]) -> Any:
        """One scope for a relation several types load: what all of them allow."""
        distinct: list[Any] = []
        for scope in scopes:
            if scope is not None and scope not in distinct:
                distinct.append(scope)
        if len(distinct) < 2:
            return distinct[0] if distinct else None
        # A scope need not be hashable; the store holds each one for good.
        key = tuple(id(scope) for scope in distinct)
        every = self.operation.every.get(key)
        if every is None:
            every = self.operation.every[key] = every_scope(
                distinct, call=self.backend.call_row_scope
            )
        return every

    def _analysis(self, key: tuple) -> dict[str, _Load]:
        operation = self.operation
        found = operation.analyses.get(key)
        if found is not None:
            return found
        _backend, ids, model, types, below = key
        selections = operation.selections[ids]
        loads: dict[str, _Load] = {}
        units = 1
        for type_name in types:
            fields = self._collect(selections, type_name)
            units += sum(len(nodes) for nodes in fields.values())
            for name, nodes in fields.items():
                field_name = _snake(name)
                if self.skips(type_name, field_name):
                    continue
                hints = self.hints(type_name, field_name)
                if hints and not hints.disable_optimization:
                    for path in hints.using or ():
                        units += self._walk(
                            loads, model, (type_name,), tuple(hint_hops(path))
                        )
                found_load = self.selected(model, type_name, field_name)
                if found_load is None:
                    continue
                load_key, relation, view = found_load
                load = self._load(loads, load_key, relation, view, type_name)
                load.scopes.append(self.edge_scope(type_name, field_name))
                deeper = [node.selection_set for node in nodes if node.selection_set]
                if deeper:
                    load.selections += deeper
                    load.types += self._child_types(type_name, name, relation.related)
        for hop_types, hops in below:
            units += self._walk(loads, model, hop_types, hops)
        operation.charge(units)
        operation.analyses[key] = loads
        return loads

    def _load(
        self,
        loads: dict[str, _Load],
        key: str,
        relation: Relation,
        view: str | None,
        owner: str | None,
    ) -> _Load:
        load = loads.get(key)
        if load is None:
            load = loads[key] = _Load(relation, owner, view)
        return load

    def _walk(
        self,
        loads: dict[str, _Load],
        model: type,
        hop_types: tuple[str | None, ...],
        hops: tuple[str, ...],
    ) -> int:
        """Plan the first of *hops*, and the rest below it; the work it took."""
        head, rest = hops[0], hops[1:]
        relation = self.relation(model, head)
        if relation is None:
            return 1
        load = self._load(loads, head, relation, None, hop_types[0])
        load.scopes += [self.edge_scope(hop_type, head) for hop_type in hop_types]
        if rest:
            types = tuple(
                dict.fromkeys(
                    found
                    for hop_type in hop_types
                    for found in self._child_types(hop_type, head, relation.related)
                )
            )
            load.below.add((types, rest))
        return len(hop_types)

    def _collect(
        self, selections: tuple[Any, ...], type_name: str | None
    ) -> dict[str, list[FieldNode]]:
        """The fields *selections* select on a row read as *type_name*, by name.

        As graphql-core collects them: a fragment applies when its type
        condition takes the type, is collected once however often it is
        spread, and ``@skip`` and ``@include`` leave out what they say. Relay
        wrappers (``edges``, ``node``, ...) are looked through.
        """
        fields: dict[str, list[FieldNode]] = {}
        visited: set[str] = set()

        def walk(selection_set: Any) -> None:
            for node in selection_set.selections:
                if not self._included(node):
                    continue
                if isinstance(node, FieldNode):
                    if not _relay_passthrough_field(node.name.value):
                        fields.setdefault(node.name.value, []).append(node)
                    elif node.selection_set is not None:
                        walk(node.selection_set)
                elif isinstance(node, InlineFragmentNode):
                    if self._applies(node.type_condition, type_name):
                        walk(node.selection_set)
                elif node.name.value not in visited:
                    visited.add(node.name.value)
                    fragment = self.fragments.get(node.name.value)
                    if fragment is not None and self._applies(
                        fragment.type_condition, type_name
                    ):
                        walk(fragment.selection_set)

        for selection_set in selections:
            walk(selection_set)
        return fields

    def _included(self, node: Any) -> bool:
        """Whether ``@skip`` and ``@include`` leave *node* in the result."""
        return self._condition(node, "skip") is not True and (
            self._condition(node, "include") is not False
        )

    def _condition(self, node: Any, directive: str) -> Any:
        """The ``if:`` *node*'s *directive* is given, or ``None`` without one.

        A variable the operation was not given reads as ``None``, which plans
        the node: loading more than the result shows is only slower.
        """
        for found in getattr(node, "directives", None) or ():
            if found.name.value != directive:
                continue
            # Validation leaves both directives exactly their one ``if:``.
            for argument in found.arguments:
                value = argument.value
                if isinstance(value, VariableNode):
                    return self.variables.get(value.name.value)
                return getattr(value, "value", None)
        return None

    def _applies(self, condition: Any, type_name: str | None) -> bool:
        """Whether a fragment on *condition* applies to a row read as *type_name*."""
        if condition is None or type_name is None:
            return True
        if condition.name.value == type_name or self.schema is None:
            return True
        abstract = self.schema.get_type(condition.name.value)
        concrete = self.schema.get_type(type_name)
        if abstract is None or concrete is None:
            return True
        return is_abstract_type(abstract) and self.schema.is_sub_type(
            abstract, concrete
        )
