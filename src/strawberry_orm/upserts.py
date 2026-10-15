"""Apply generated upsert inputs: find-or-create, governed by each projection.

``orm.apply_upsert()`` and the ``upsert`` arm of ``orm.apply_ref_list()`` run
here. The walk reads the policy the generated input types carry
(``__upsert_policy__`` and ``__relation_policy__``), so a payload built in
Python is held to the same rules as one the schema validated.

Every payload is validated in full before the first write. The walk itself is
a generator: each backend call returns a value or an awaitable and is yielded
to a driver, so one walk serves both sync and async sessions.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import cache
from typing import Any

import strawberry
from strawberry.relay import GlobalID

from strawberry_orm.repo import AbstractRepo, _check_auth

_OPERATIONS = ("create", "update", "upsert", "unlink", "delete")

Steps = Generator[Any, Any, Any]


class UpsertValidationError(ValueError):
    """A nested write payload breaks the rules of its projection."""


class UpsertNotFoundError(LookupError):
    """An upsert without ``create`` matched nothing, or an id named no row."""


def run_sync(steps: Steps) -> Any:
    """Drive *steps* on a sync session."""
    value = None
    try:
        while True:
            effect = steps.send(value)
            if inspect.isawaitable(effect):
                if inspect.iscoroutine(effect):
                    effect.close()
                steps.close()
                raise TypeError(
                    "A repo hook returned an awaitable during a write on a sync "
                    "session; make it synchronous"
                )
            value = effect
    except StopIteration as stop:
        return stop.value


async def run_async(steps: Steps) -> Any:
    """Drive *steps* on an async session."""
    value = None
    try:
        while True:
            effect = steps.send(value)
            value = await effect if inspect.isawaitable(effect) else effect
    except StopIteration as stop:
        return stop.value


@dataclass(frozen=True)
class _Parent:
    """The row a nested to-many item hangs off. ``instance`` is unset while validating."""

    model: type
    spec: Any
    instance: Any = None


def _is_set(value: Any) -> bool:
    return value is not strawberry.UNSET and value is not None


def _unwrap(field_type: Any) -> Any:
    while hasattr(field_type, "of_type"):
        field_type = field_type.of_type
    return field_type


@cache
def _declared_fields(cls: type) -> dict[str, Any]:
    return {
        field.python_name: _unwrap(field.type)
        for field in cls.__strawberry_definition__.fields  # type: ignore[attr-defined]
    }


def _type_name(cls: type) -> str:
    return cls.__strawberry_definition__.name  # type: ignore[attr-defined]


def _scalars(payload: Any, cls: type) -> dict[str, Any]:
    relations = cls.__orm_relations__  # type: ignore[attr-defined]
    return {
        name: getattr(payload, name)
        for name in cls.__dataclass_fields__  # type: ignore[attr-defined]
        if name != "id"
        and name not in relations
        and getattr(payload, name) is not strawberry.UNSET
    }


def _expect(value: Any, cls: type) -> None:
    if not isinstance(value, cls):
        raise UpsertValidationError(
            f"Expected {_type_name(cls)}, got {type(value).__name__}"
        )


def _selected_operation(payload: Any, cls: type) -> tuple[str, Any]:
    policy = cls.__relation_policy__  # type: ignore[attr-defined]
    sent = [op for op in _OPERATIONS if _is_set(getattr(payload, op, strawberry.UNSET))]
    if len(sent) != 1:
        allowed = ", ".join(op for op in _OPERATIONS if op in policy["allowed_ops"])
        raise UpsertValidationError(
            f"{_type_name(cls)} takes exactly one of {allowed}; "
            f"got {', '.join(sent) or 'none'}"
        )
    operation = sent[0]
    if operation not in policy["allowed_ops"]:
        raise UpsertValidationError(
            f"{_type_name(cls)} does not allow the {operation} operation"
        )
    return operation, getattr(payload, operation)


def has_upsert(ref: Any) -> bool:
    """Whether *ref* selects the ``upsert`` operation."""
    return _is_set(getattr(ref, "upsert", strawberry.UNSET))


class UpsertWriter:
    """Validates and applies one nested write for a single call."""

    def __init__(
        self,
        backend: Any,
        ops: Any,
        info: Any,
        authorize: Callable[..., bool] | None = None,
    ) -> None:
        self._backend = backend
        self._ops = ops
        self._info = info
        self._authorize = authorize
        self._repos: dict[type, AbstractRepo] = {}

    # -- Entry points ----------------------------------------------------------

    def validate_root(self, payload: Any) -> None:
        cls = type(payload)
        if getattr(cls, "__upsert_policy__", None) is None:
            raise TypeError(
                "apply_upsert() takes an input built by "
                "orm.mutations.upsert_node_input()"
            )
        self._validate_upsert(payload, cls, (), None)

    def apply_root(self, payload: Any) -> Steps:
        instance, _matched = yield from self._upsert(payload, type(payload), None)
        return instance

    def validate_refs(self, parent_model: type, spec: Any, refs: list[Any]) -> None:
        """Check generated list items before ``apply_ref_list`` writes anything."""
        parent = _Parent(parent_model, spec)
        for ref in refs:
            ref_cls = type(ref)
            if getattr(ref_cls, "__relation_policy__", None) is not None:
                self._validate_container(ref, ref_cls, (parent_model,), parent)
            elif has_upsert(ref):
                upsert_cls = type(ref.upsert)
                if getattr(upsert_cls, "__upsert_policy__", None) is None:
                    raise TypeError(
                        f"The upsert arm of {ref_cls.__name__} must be built by "
                        f"orm.mutations; got {upsert_cls.__name__}"
                    )
                self._validate_upsert(ref.upsert, upsert_cls, (parent_model,), parent)

    def apply_ref_upsert(
        self, parent_instance: Any, spec: Any, ref: Any, *, link: bool = False
    ) -> Steps:
        """Resolve the upsert arm of an ``apply_ref_list`` item.

        With *link*, the row is also linked to *parent_instance*; otherwise the
        caller links it.
        """
        parent = _Parent(type(parent_instance), spec, parent_instance)
        instance, matched = yield from self._upsert(
            ref.upsert, type(ref.upsert), parent
        )
        if matched:
            self._check_repo(
                type(instance), "can_link", parent_instance, spec.name, instance
            )
        if link:
            yield self._ops.link(parent_instance, spec, instance)
        return instance

    # -- Validation (no I/O) ---------------------------------------------------

    def _visit(
        self, model: type, max_visits: int | None, path: tuple[type, ...]
    ) -> tuple[type, ...]:
        visits = path.count(model) + 1
        if max_visits is not None and visits > max_visits:
            raise UpsertValidationError(
                f"{model.__name__} appears {visits} times on one nesting path; "
                f"its projection allows at most {max_visits} (maxVisits)"
            )
        return (*path, model)

    def _validate_upsert(
        self,
        payload: Any,
        cls: type,
        path: tuple[type, ...],
        parent: _Parent | None,
    ) -> None:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        policy = cls.__upsert_policy__  # type: ignore[attr-defined]
        path = self._visit(model, policy["max_visits"], path)
        where = self._where_values(payload, cls)
        fields = _declared_fields(cls)

        update = getattr(payload, "update", strawberry.UNSET)
        if _is_set(update):
            if policy["on_match"] != "UPDATE":
                raise UpsertValidationError(
                    f"{_type_name(cls)} takes no update: matched "
                    f"{model.__name__} rows are not modified"
                )
            _expect(update, fields["update"])
            self._validate_model_input(update, fields["update"], path, parent)

        create = getattr(payload, "create", strawberry.UNSET)
        if _is_set(create):
            _expect(create, fields["create"])
            for name, value in where.items():
                sent = getattr(create, name, strawberry.UNSET)
                if name != "id" and sent is not strawberry.UNSET and sent != value:
                    raise UpsertValidationError(
                        f"{_type_name(cls)}: where.{name} and create.{name} differ"
                    )
            self._validate_model_input(create, fields["create"], path, parent)

    def _validate_model_input(
        self,
        payload: Any,
        cls: type,
        path: tuple[type, ...],
        parent: _Parent | None,
    ) -> None:
        if parent is not None and parent.spec.relation_mode == "reverse_fk":
            for attr in self._ops.back_reference(parent.model, parent.spec):
                if attr is not None and _is_set(
                    getattr(payload, attr, strawberry.UNSET)
                ):
                    raise UpsertValidationError(
                        f"{_type_name(cls)}.{attr} cannot be set: the row is nested "
                        f"under {parent.model.__name__}.{parent.spec.name}"
                    )

        fields = _declared_fields(cls)
        model = cls.__orm_model__  # type: ignore[attr-defined]
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            value = getattr(payload, name, strawberry.UNSET)
            if not _is_set(value):
                continue
            if spec.kind == "single":
                self._validate_container(value, fields[name], path, None)
                continue
            if not isinstance(value, (list, tuple)):
                raise UpsertValidationError(
                    f"{_type_name(cls)}.{name} must be a list of {_type_name(fields[name])}"
                )
            child_parent = _Parent(model, spec)
            for item in value:
                self._validate_container(item, fields[name], path, child_parent)

    def _validate_container(
        self,
        payload: Any,
        cls: type,
        path: tuple[type, ...],
        parent: _Parent | None,
    ) -> None:
        _expect(payload, cls)
        operation, value = _selected_operation(payload, cls)
        policy = cls.__relation_policy__  # type: ignore[attr-defined]
        model = cls.__orm_model__  # type: ignore[attr-defined]
        fields = _declared_fields(cls)

        on_replace = getattr(payload, "on_replace", strawberry.UNSET)
        if _is_set(on_replace):
            on_replace = getattr(on_replace, "value", on_replace)
            if on_replace not in policy["on_replace_options"]:
                raise UpsertValidationError(
                    f"{_type_name(cls)} does not allow onReplace {on_replace}"
                )

        if operation == "upsert":
            _expect(value, fields["upsert"])
            self._validate_upsert(value, fields["upsert"], path, parent)
        elif operation in ("create", "update"):
            _expect(value, fields[operation])
            child_path = self._visit(model, policy["max_visits"], path)
            if operation == "update":
                self._pk(value.id, model)
            self._validate_model_input(value, fields[operation], child_path, parent)
        else:
            self._pk(value.id, model)
            if (
                operation == "unlink"
                and parent is not None
                and parent.spec.relation_mode == "reverse_fk"
                and not parent.spec.nullable
            ):
                raise UpsertValidationError(
                    f"{model.__name__} cannot be unlinked from "
                    f"{parent.model.__name__}.{parent.spec.name}: its foreign key "
                    f"is not nullable"
                )

    def _where_values(self, payload: Any, cls: type) -> dict[str, Any]:
        where_cls = _declared_fields(cls)["where"]
        where = getattr(payload, "where", strawberry.UNSET)
        if not _is_set(where):
            raise UpsertValidationError(f"{_type_name(cls)} requires where")
        _expect(where, where_cls)
        values = {
            name: getattr(where, name)
            for name in where_cls.__dataclass_fields__
            if getattr(where, name) is not strawberry.UNSET
        }
        if not values:
            raise UpsertValidationError(
                f"{_type_name(where_cls)} needs at least one field"
            )
        if "id" in values:
            values["id"] = self._pk(values["id"], cls.__orm_model__)  # type: ignore[attr-defined]
        return values

    def _pk(self, raw: Any, model: type) -> Any:
        """Accept a raw primary key or a Relay global ID for *model*."""
        if raw is None:
            raise UpsertValidationError(f"A {model.__name__} id cannot be null")
        if isinstance(raw, str):
            try:
                global_id = GlobalID.from_id(raw)
            except ValueError:
                pass
            else:
                registry = self._backend._type_registry
                owner = registry.get(global_id.type_name)
                if owner is model:
                    raw = global_id.node_id
                elif owner is not None:
                    raise UpsertValidationError(
                        f"{raw!r} is a global ID for {global_id.type_name}, "
                        f"not {model.__name__}"
                    )
        return self._ops.coerce_pk(model, raw)

    # -- Writes ----------------------------------------------------------------

    def _repo(self, model: type) -> AbstractRepo:
        if model not in self._repos:
            repo = self._backend.get_repo(model)
            if repo is None:
                repo = AbstractRepo(self._backend)
                repo.model = model
            self._repos[model] = repo
        return self._repos[model]

    def _check_repo(self, model: type, method: str, *args: Any) -> None:
        _check_auth(self._repo(model), method, *args, self._info)

    def _check_authorize(self, action: str, model: type, pk: Any) -> None:
        if self._authorize is not None and not self._authorize(
            action, model, pk, self._info
        ):
            raise PermissionError(
                f"authorize denied the {action} operation on {model.__name__}"
            )

    def _upsert(
        self, payload: Any, cls: type, parent: _Parent | None
    ) -> Generator[Any, Any, tuple[Any, bool]]:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        policy = cls.__upsert_policy__  # type: ignore[attr-defined]
        fields = _declared_fields(cls)
        where = self._where_values(payload, cls)
        query = dict(where)
        if parent is not None:
            query.update(self._ops.parent_scope(parent.instance, parent.spec))

        repo = self._repo(model)
        instance = yield self._ops.find(repo, model, query)
        if instance is None:
            create = getattr(payload, "create", strawberry.UNSET)
            if not _is_set(create):
                raise UpsertNotFoundError(
                    f"No {model.__name__} matches {where} and no create was sent"
                )
            seed = {name: value for name, value in where.items() if name != "id"}
            created = yield from self._create(create, fields["create"], parent, seed)
            return created, False

        self._check_authorize("upsert", model, self._ops.pk_value(instance))
        self._check_repo(model, "can_upsert", instance)
        yield repo.on_match(instance, self._info)

        if policy["on_match"] == "UPDATE":
            update = getattr(payload, "update", strawberry.UNSET)
            if _is_set(update):
                yield from self._update(instance, update, fields["update"])
        elif policy["on_match"] == "APPEND":
            create = getattr(payload, "create", strawberry.UNSET)
            if _is_set(create):
                yield from self._append(instance, create, fields["create"], policy)
        return instance, True

    def _create(
        self,
        payload: Any,
        cls: type,
        parent: _Parent | None,
        seed: dict[str, Any],
    ) -> Steps:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        fields = _declared_fields(cls)
        data = {**seed, **_scalars(payload, cls)}
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            value = getattr(payload, name, strawberry.UNSET)
            if spec.kind == "single" and _is_set(value):
                data[name] = yield from self._link_target(value, fields[name])
        if parent is not None:
            data.update(self._ops.parent_assignment(parent.instance, parent.spec))

        self._check_authorize("create", model, None)
        repo = self._repo(model)
        data = repo.on_before_create(data, self._info)
        self._check_repo(model, "can_create", data)
        instance = yield self._ops.create(repo, model, data)
        repo.on_after_create(instance, self._info)
        yield from self._apply_to_many(instance, payload, cls, created=True)
        return instance

    def _update(self, instance: Any, payload: Any, cls: type) -> Steps:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        fields = _declared_fields(cls)
        repo = self._repo(model)
        data = repo.on_before_update(instance, _scalars(payload, cls), self._info)
        self._check_repo(model, "can_update", instance, data)
        for name, value in data.items():
            setattr(instance, name, value)

        replaced: list[Any] = []
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            link = getattr(payload, name, strawberry.UNSET)
            if spec.kind != "single" or not _is_set(link):
                continue
            target = yield from self._link_target(link, fields[name])
            previous = yield self._ops.load_attr(instance, name)
            setattr(instance, name, target)
            if (
                previous is not None
                and self._ops.pk_value(previous) != self._ops.pk_value(target)
                and self._on_replace(link, fields[name]) == "DELETE"
            ):
                replaced.append(previous)

        # Saved before the old targets go, so a cascade cannot take this row too.
        yield self._ops.save(repo, instance)
        for previous in replaced:
            yield from self._delete_instance(previous)
        repo.on_after_update(instance, self._info)
        yield from self._apply_to_many(instance, payload, cls, created=False)
        return instance

    def _append(
        self, instance: Any, payload: Any, cls: type, policy: dict[str, Any]
    ) -> Steps:
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            if spec.kind != "single" or not _is_set(
                getattr(payload, name, strawberry.UNSET)
            ):
                continue
            if policy["on_match_links"] == "IGNORE":
                continue
            raise UpsertValidationError(
                f"{_type_name(cls)}.{name} cannot be applied to a matched "
                f"{type(instance).__name__}: forward links are only written when "
                f"the row is created"
            )
        yield from self._apply_to_many(instance, payload, cls, created=False)

    def _apply_to_many(
        self, instance: Any, payload: Any, cls: type, *, created: bool
    ) -> Steps:
        fields = _declared_fields(cls)
        model = cls.__orm_model__  # type: ignore[attr-defined]
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            items = getattr(payload, name, strawberry.UNSET)
            if spec.kind != "many" or not _is_set(items):
                continue
            if created:
                self._ops.init_collection(instance, name)
            parent = _Parent(model, spec, instance)
            for item in items:
                yield from self._apply_item(parent, item, fields[name])

    def _apply_item(self, parent: _Parent, item: Any, cls: type) -> Steps:
        operation, value = _selected_operation(item, cls)
        fields = _declared_fields(cls)
        model = cls.__orm_model__  # type: ignore[attr-defined]
        if operation == "create":
            child = yield from self._create(value, fields["create"], parent, {})
            yield self._ops.link(parent.instance, parent.spec, child)
        elif operation == "upsert":
            child, matched = yield from self._upsert(value, fields["upsert"], parent)
            if matched:
                self._check_repo(
                    model, "can_link", parent.instance, parent.spec.name, child
                )
            yield self._ops.link(parent.instance, parent.spec, child)
        elif operation == "update":
            child = yield from self._update_by_id(value, fields["update"])
            self._check_repo(
                model, "can_link", parent.instance, parent.spec.name, child
            )
            yield self._ops.link(parent.instance, parent.spec, child)
        else:
            yield from self._remove_member(parent, operation, value.id, model)

    def _remove_member(
        self, parent: _Parent, operation: str, raw_id: Any, model: type
    ) -> Steps:
        pk = self._pk(raw_id, model)
        self._check_authorize(operation, model, pk)
        child = yield self._ops.get(self._repo(model), model, pk)
        if child is not None:
            is_member = yield self._ops.is_member(parent.instance, parent.spec, child)
            if not is_member:
                child = None
        if child is None:
            raise UpsertNotFoundError(
                f"No {model.__name__} {pk!r} in "
                f"{parent.model.__name__}.{parent.spec.name}"
            )
        if operation == "unlink":
            self._check_repo(
                model, "can_unlink", parent.instance, parent.spec.name, child
            )
            yield self._ops.unlink(parent.instance, parent.spec, child)
        else:
            yield from self._delete_instance(child)

    def _delete_instance(self, instance: Any) -> Steps:
        model = type(instance)
        repo = self._repo(model)
        self._check_repo(model, "can_delete", instance)
        repo.on_before_delete(instance, self._info)
        yield self._ops.delete(repo, instance)

    def _update_by_id(self, payload: Any, cls: type) -> Steps:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        pk = self._pk(payload.id, model)
        self._check_authorize("update", model, pk)
        instance = yield self._ops.get(self._repo(model), model, pk)
        if instance is None:
            raise UpsertNotFoundError(f"No {model.__name__} {pk!r}")
        return (yield from self._update(instance, payload, cls))

    def _link_target(self, link: Any, cls: type) -> Steps:
        operation, value = _selected_operation(link, cls)
        fields = _declared_fields(cls)
        if operation == "create":
            return (yield from self._create(value, fields["create"], None, {}))
        if operation == "upsert":
            instance, _matched = yield from self._upsert(value, fields["upsert"], None)
            return instance
        return (yield from self._update_by_id(value, fields["update"]))

    def _on_replace(self, link: Any, cls: type) -> str:
        sent = getattr(link, "on_replace", strawberry.UNSET)
        if _is_set(sent):
            return getattr(sent, "value", sent)
        return cls.__relation_policy__["default_on_replace"]  # type: ignore[attr-defined]
