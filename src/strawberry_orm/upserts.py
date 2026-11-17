"""Apply generated upsert inputs and ref lists, governed by each projection.

``orm.apply_upsert()`` and every item of ``orm.apply_ref_list()`` run here.
The walk reads the policy the generated input types carry
(``__upsert_policy__`` and ``__relation_policy__``), so a payload built in
Python is held to the same rules as one the schema validated.

Every payload is validated in full before the first write. The walk itself is
a generator: each backend call, repo hook and ``authorize`` call returns a
value or an awaitable and is yielded to a driver, so one walk serves both
sync and async sessions.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import cache
from typing import Any, NoReturn

import strawberry
from strawberry.relay import GlobalID

from strawberry_orm.backends._base import input_to_dict
from strawberry_orm.repo import AbstractRepo, _allows, _require

_OPERATIONS = ("create", "update", "upsert", "unlink", "delete")
_PLAIN_OPERATIONS = ("create", "update", "unlink", "delete")
#: The ``on_after_*`` hooks a repo inherits, which do nothing.
_NO_AFTER_HOOK = (AbstractRepo.on_after_create, AbstractRepo.on_after_update)

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
                    "A repo hook or authorize callback returned an awaitable "
                    "during a write on a sync session; make it synchronous"
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


def _plain_operation(ref: Any) -> tuple[str, Any]:
    """The one operation a hand-built ``orm.ref()`` item selects."""
    sent = [
        (op, getattr(ref, op))
        for op in _PLAIN_OPERATIONS
        if _is_set(getattr(ref, op, strawberry.UNSET))
    ]
    if len(sent) != 1:
        raise UpsertValidationError(
            f"{type(ref).__name__} takes exactly one of "
            f"{', '.join(_PLAIN_OPERATIONS)}; got "
            f"{', '.join(op for op, _value in sent) or 'none'}"
        )
    return sent[0]


def has_upsert(ref: Any) -> bool:
    """Whether *ref* selects the ``upsert`` operation."""
    return _is_set(getattr(ref, "upsert", strawberry.UNSET))


def _finds_its_own(repo: AbstractRepo) -> bool:
    """Whether *repo* overrides how an upsert finds the row it matches."""
    cls = type(repo)
    return (
        cls.find_for_upsert is not AbstractRepo.find_for_upsert
        or cls.find_for_upsert_async is not AbstractRepo.find_for_upsert_async
    )


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
        self._after: list[tuple[Callable[..., Any], Any]] = []

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
        yield from self._run_after_hooks()
        return instance

    def validate_refs(self, parent_model: type, spec: Any, refs: list[Any]) -> None:
        """Check every list item before ``apply_ref_list`` writes anything."""
        if spec.kind != "many":
            raise TypeError(
                f"apply_ref_list() takes a to-many relation; "
                f"{parent_model.__name__}.{spec.name} links a single row"
            )
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
            elif _plain_operation(ref)[0] == "unlink":
                self._validate_unlink(spec.related_model, parent)

    def apply_refs(self, parent_instance: Any, spec: Any, refs: list[Any]) -> Steps:
        """Apply validated ``apply_ref_list`` items to *parent_instance*, in order.

        A plain item that ``authorize`` denies is skipped, and so is an update
        of a row the repo cannot see; every other refusal raises.
        """
        parent = _Parent(type(parent_instance), spec, parent_instance)
        for ref in refs:
            if has_upsert(ref):
                child, matched = yield from self._upsert(
                    ref.upsert, type(ref.upsert), parent
                )
                if matched:
                    yield from self._check_link(parent, child)
                else:
                    yield from self._check_joined(parent, child)
                yield self._ops.link(parent_instance, spec, child)
            else:
                yield from self._apply_plain_ref(parent, *_plain_operation(ref))
        yield from self._run_after_hooks()

    def _after_write(self, hook: Callable[..., Any], instance: Any) -> None:
        """Run an ``on_after_*`` *hook* once the whole write has passed its checks.

        A check later in the same write can still refuse it and roll it back,
        and the side effects the hooks are for cannot be rolled back with it.
        """
        if getattr(hook, "__func__", None) not in _NO_AFTER_HOOK:
            self._after.append((hook, instance))

    def _run_after_hooks(self) -> Steps:
        after, self._after = self._after, []
        if after:
            # The database checks some of the write only as it is flushed or
            # committed, which would be after the hooks.
            yield self._ops.check_constraints()
        for hook, instance in after:
            yield hook(instance, self._info)

    # -- Validation (no I/O) ---------------------------------------------------

    def _visit(
        self, model: type, max_visits: int | None, path: tuple[type, ...]
    ) -> tuple[type, ...]:
        visits = path.count(model) + 1
        if max_visits is not None and visits > max_visits:
            raise UpsertValidationError(
                f"{self._called(model, 'One type')} appears {visits} times on one "
                f"nesting path; its projection allows at most {max_visits} (maxVisits)"
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
                    f"{_type_name(cls)} takes no update: a matched "
                    f"{self._called(model, 'row')} is not modified"
                )
            _expect(update, fields["update"])
            self._validate_model_input(update, fields["update"], path, parent)

        create = getattr(payload, "create", strawberry.UNSET)
        if _is_set(create):
            _expect(create, fields["create"])
            creatable = fields["create"].__dataclass_fields__
            for name, value in where.items():
                if name == "id":
                    continue
                # A miss seeds the new row from where, so a field create does
                # not offer would be written past its allowlist.
                if name not in creatable:
                    raise UpsertValidationError(
                        f"{_type_name(cls)}: where.{name} cannot be sent with "
                        f"create, because a new {self._called(model, 'row')} would "
                        f"be written with it and create does not allow {name}"
                    )
                sent = getattr(create, name, strawberry.UNSET)
                if sent is not strawberry.UNSET and sent != value:
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
                        f"under {self._relation(parent)}"
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
            if operation == "unlink" and parent is not None:
                self._validate_unlink(model, parent)

    def _validate_unlink(self, model: type, parent: _Parent) -> None:
        unlinked, relation = self._called(model, "A row"), self._relation(parent)
        if parent.spec.deletes_orphans:
            raise UpsertValidationError(
                f"{unlinked} cannot be unlinked from {relation}: {relation} "
                f"deletes the rows it lets go of, so send delete instead"
            )
        if parent.spec.relation_mode == "reverse_fk" and not parent.spec.nullable:
            raise UpsertValidationError(
                f"{unlinked} cannot be unlinked from {relation}: its foreign key "
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
            named = self._backend.type_name_of(model)
            raise UpsertValidationError(
                f"A {named} id cannot be null" if named else "An id cannot be null"
            )
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
                        f"not {self._called(model, 'the type this id is for')}"
                    )
        return self._ops.coerce_pk(model, raw)

    def _called(self, model: type, otherwise: str) -> str:
        """What a client calls *model*'s rows, or *otherwise* if no type says."""
        return self._backend.type_name_of(model) or otherwise

    def _relation(self, parent: _Parent) -> str:
        """*parent*'s relation, named after the type a client knows its rows by."""
        named = self._backend.type_name_of(parent.model)
        return f"{named}.{parent.spec.name}" if named else parent.spec.name

    # -- Writes ----------------------------------------------------------------

    def _repo(self, model: type) -> AbstractRepo:
        if model not in self._repos:
            repo = self._backend.get_repo(model)
            if repo is None:
                repo = AbstractRepo(self._backend)
                repo.model = model
            self._repos[model] = repo
        return self._repos[model]

    def _owner(self, model: type, row: Any) -> type:
        """The model whose repo governs *row*, a row read as a *model* row."""
        return self._owner_of(model, type(row))

    def _owner_of(self, model: type, row_model: type) -> type:
        """The model whose repo governs a *row_model* row, read as a *model* row.

        A row of a subclass is its own model's, unless that model's repo is
        *model*'s exactly: the same class, for the same model. One repo class
        registered for both, or the one ``policy=`` makes, can answer for
        each model differently, so its ``self.model`` must be the row's.
        """
        if row_model is model:
            return model
        mine, base = self._repo(row_model), self._repo(model)
        if type(mine) is type(base) and mine.model is base.model:
            return model
        return row_model

    def _row_model(self, model: type, row: Any) -> Generator[Any, Any, type | None]:
        """The model *row*, read as a *model* row, is a row of.

        A Django multi-table child, or a SQLAlchemy joined-table subclass
        without a discriminator, reads as a row of its base, and only its own
        table holds it apart. Where such a subclass has a repo of its own, a
        row its table holds is its own: the deepest such subclass's. ``None``
        when two of them, neither inheriting the other, hold it, so no one
        repo can answer for it.
        """
        loaded = type(row)
        apart = self._backend.classes_written_apart(
            self._repo(model), self._backend.subclasses_read_as_base(loaded)
        )
        if not apart:
            return loaded
        holders = yield self._ops.holders(row, list(apart))
        if not holders:
            return loaded
        deepest = max(holders, key=lambda holder: len(holder.__mro__))
        if all(issubclass(deepest, holder) for holder in holders):
            return deepest
        return None

    def _owned(self, model: type, row: Any) -> Generator[Any, Any, tuple[type, Any]]:
        """*row*, read through *model*'s repo, and the model whose repo owns it.

        The lookup applied only *model*'s ``scope_query``; a subclass row with
        a repo of its own reads as absent unless that repo can see it too.
        """
        if row is None:
            return model, None
        row_model = yield from self._row_model(model, row)
        if row_model is None:
            return model, None
        owner = self._owner_of(model, row_model)
        if owner is model:
            return model, row
        pk = self._ops.pk_value(row)
        return owner, (yield self._ops.get(self._repo(owner), owner, pk))

    def _as_own(self, model: type, row: Any) -> Generator[Any, Any, tuple[type, Any]]:
        """*row*, read off a link to *model*, as a row of its own model, and that model.

        Read off the link, no repo narrowed it, and none narrows it here.
        """
        row_model = yield from self._row_model(model, row)
        if row_model is None:
            raise PermissionError(
                f"The {self._called(model, 'row')} the link held is a row of more "
                f"than one subclass with a repo of its own, so none can let it go"
            )
        if row_model is not type(row):
            unscoped = AbstractRepo(self._backend)
            unscoped.model = row_model
            pk = self._ops.pk_value(row)
            row = yield self._ops.get(unscoped, row_model, pk)
        return self._owner_of(model, row_model), row

    def _get(self, model: type, pk: Any) -> Generator[Any, Any, tuple[type, Any]]:
        """The *model* row *pk* names, if the repos let the caller see it, and its owner."""
        row = yield self._ops.get(self._repo(model), model, pk)
        return (yield from self._owned(model, row))

    def _check_repo(self, model: type, method: str, *args: Any) -> Steps:
        allowed = yield getattr(self._repo(model), method)(*args, self._info)
        _require(allowed, f"AbstractRepo.{method}")

    def _authorized(self, action: str, model: type, pk: Any) -> Steps:
        if self._authorize is None:
            return True
        allowed = yield self._authorize(action, model, pk, self._info)
        return _allows(allowed, "authorize")

    def _check_authorize(self, action: str, model: type, pk: Any) -> Steps:
        if not (yield from self._authorized(action, model, pk)):
            self._authorize_denied(action, model)

    def _authorize_denied(self, action: str, model: type) -> NoReturn:
        named = self._backend.type_name_of(model)
        on = f" on {named}" if named else ""
        raise PermissionError(f"authorize denied the {action} operation{on}")

    def _denied(
        self, action: str, model: type, row: Any
    ) -> Generator[Any, Any, type | None]:
        """The model ``authorize`` denies *action* on *row* as, a row read as *model*'s.

        Asked once the row is read, so it is told the key as the row stores
        it, not the caller's spelling of it, which the database may match
        regardless of case or padding; and never about a row the repos hide.
        A row of a subclass is asked about again as its own model's.
        """
        pk = self._ops.pk_value(row)
        for asked in dict.fromkeys((model, type(row))):
            if not (yield from self._authorized(action, asked, pk)):
                return asked
        return None

    def _check_allowed(self, action: str, model: type, row: Any) -> Steps:
        denied = yield from self._denied(action, model, row)
        if denied is not None:
            self._authorize_denied(action, denied)

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
        found = yield self._ops.find(repo, model, query)
        if found is not None and _finds_its_own(repo):
            # Every row a write finds goes through scope_query, the rows an
            # override of find_for_upsert finds included.
            owner, instance = yield from self._get(model, self._ops.pk_value(found))
        else:
            owner, instance = yield from self._owned(model, found)
        if instance is None:
            create = getattr(payload, "create", strawberry.UNSET)
            if not _is_set(create):
                raise UpsertNotFoundError(
                    f"No {self._called(model, 'row')} matches {where} and no "
                    f"create was sent"
                )
            seed = {name: value for name, value in where.items() if name != "id"}
            created = yield from self._create(create, fields["create"], parent, seed)
            return created, False

        yield from self._check_authorize(
            "upsert", type(instance), self._ops.pk_value(instance)
        )
        yield from self._check_repo(owner, "can_upsert", instance)
        yield self._repo(owner).on_match(instance, self._info)

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
        linked: list[tuple[Any, Any, Any]] = []
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            value = getattr(payload, name, strawberry.UNSET)
            if spec.kind == "single" and _is_set(value):
                target, found = yield from self._link_target(value, fields[name])
                data[name] = target
                holder = None
                if found:
                    # Looked up now: once the row is written, it holds the target.
                    holder = yield from self._holder(model, spec, target)
                    if holder is not None and spec.relation_mode == "forward_fk":
                        # Building the row already lets the holder go.
                        yield from self._check_taken(model, spec, holder, target)
                        holder = None
                linked.append((spec, target, holder))
        if parent is not None:
            data.update(self._ops.parent_assignment(parent.instance, parent.spec))

        yield from self._check_authorize("create", model, None)
        repo = self._repo(model)
        data = yield repo.on_before_create(data, self._info)
        yield from self._check_repo(model, "can_create", data)
        instance = yield self._ops.create(repo, model, data)
        for spec, target, holder in linked:
            yield from self._check_repo(
                self._owner(spec.related_model, target),
                "can_link",
                instance,
                spec.name,
                target,
            )
            if holder is not None:
                yield from self._check_taken(model, spec, holder, target)
        self._after_write(repo.on_after_create, instance)
        yield from self._apply_to_many(instance, payload, cls, created=True)
        return instance

    def _update(self, instance: Any, payload: Any, cls: type) -> Steps:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        fields = _declared_fields(cls)
        owner = self._owner(model, instance)
        repo = self._repo(owner)
        data = yield repo.on_before_update(instance, _scalars(payload, cls), self._info)
        yield from self._check_repo(owner, "can_update", instance, data)
        for name, value in data.items():
            setattr(instance, name, value)

        replaced: list[Any] = []
        for name, spec in cls.__orm_relations__.items():  # type: ignore[attr-defined]
            link = getattr(payload, name, strawberry.UNSET)
            if spec.kind != "single" or not _is_set(link):
                continue
            target, found = yield from self._link_target(link, fields[name])
            previous = yield self._ops.load_attr(instance, name)
            if not self._same_row(previous, target):
                yield from self._check_repo(
                    self._owner(spec.related_model, target),
                    "can_link",
                    instance,
                    name,
                    target,
                )
                if found:
                    holder = yield from self._holder(model, spec, target)
                    if holder is not None and not self._same_row(holder, instance):
                        yield from self._check_taken(model, spec, holder, target)
                if previous is not None:
                    held_by, previous = yield from self._as_own(
                        spec.related_model, previous
                    )
                    yield from self._check_repo(
                        held_by, "can_unlink", instance, name, previous
                    )
                    if spec.deletes_orphans:
                        yield from self._check_orphaned(spec.related_model, previous)
                    elif self._on_replace(link, fields[name]) == "DELETE":
                        replaced.append((spec.related_model, previous))
            setattr(instance, name, target)

        # Saved before the old targets go, so a cascade cannot take this row too.
        yield self._ops.save(repo, instance)
        for related_model, previous in replaced:
            yield from self._delete_replaced(related_model, previous)
        self._after_write(repo.on_after_update, instance)
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
                f"{self._called(type(instance), 'row')}: forward links are only "
                f"written when the row is created"
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
            yield from self._check_joined(parent, child)
        elif operation == "upsert":
            child, matched = yield from self._upsert(value, fields["upsert"], parent)
            if matched:
                yield from self._check_link(parent, child)
            else:
                yield from self._check_joined(parent, child)
        elif operation == "update":
            child = yield from self._update_by_id(value, fields["update"])
            yield from self._check_link(parent, child)
        else:
            pk = self._pk(value.id, model)
            owner, child = yield from self._member(parent, pk, model)
            yield from self._check_allowed(operation, model, child)
            yield from self._remove_member(parent, operation, owner, child)
            return
        yield self._ops.link(parent.instance, parent.spec, child)

    def _apply_plain_ref(self, parent: _Parent, operation: str, value: Any) -> Steps:
        model = parent.spec.related_model
        repo = self._repo(model)
        if operation == "create":
            if not (yield from self._authorized("create", model, None)):
                return
            data = {
                **input_to_dict(value),
                **self._ops.parent_assignment(parent.instance, parent.spec),
            }
            data = yield repo.on_before_create(data, self._info)
            yield from self._check_repo(model, "can_create", data)
            child = yield self._ops.create(repo, model, data)
            self._after_write(repo.on_after_create, child)
            yield from self._check_joined(parent, child)
        elif operation == "update":
            data = input_to_dict(value)
            pk = self._pk(data.pop("id"), model)
            owner, child = yield from self._get(model, pk)
            if child is None:
                return
            if (yield from self._denied("update", model, child)) is not None:
                return
            repo = self._repo(owner)
            data = yield repo.on_before_update(child, data, self._info)
            yield from self._check_repo(owner, "can_update", child, data)
            yield from self._check_link(parent, child)
            if data:
                for name, field_value in data.items():
                    setattr(child, name, field_value)
                yield self._ops.save(repo, child)
                self._after_write(repo.on_after_update, child)
        else:
            pk = self._pk(value.id, model)
            owner, child = yield from self._member(parent, pk, model)
            if (yield from self._denied(operation, model, child)) is not None:
                return
            yield from self._remove_member(parent, operation, owner, child)
            return
        yield self._ops.link(parent.instance, parent.spec, child)

    def _check_joined(self, parent: _Parent, child: Any) -> Steps:
        """Ask whether *child*, created under *parent*, may join it.

        Through a many-to-many list ``can_create`` is not told the parent, so
        only ``can_link`` can refuse the row its place there.
        """
        yield from self._check_repo(
            self._owner(parent.spec.related_model, child),
            "can_link",
            parent.instance,
            parent.spec.name,
            child,
        )

    def _check_link(self, parent: _Parent, child: Any) -> Steps:
        """Ask whether *child* may join *parent*, and leave the row it is under."""
        model = self._owner(parent.spec.related_model, child)
        yield from self._check_repo(
            model, "can_link", parent.instance, parent.spec.name, child
        )
        previous = yield self._ops.previous_parent(parent.instance, parent.spec, child)
        if previous is not None:
            yield from self._check_repo(
                model, "can_unlink", previous, parent.spec.name, child
            )

    def _holder(self, model: type, spec: Any, target: Any) -> Steps:
        """The *model* row a single link's *target* is taken from, if any.

        A reverse one-to-one moves its target: the foreign key is on the
        target, so pointing a row at it takes it from the row it pointed at.
        So does a forward link whose target holds one row back (``taken_from``):
        the ORM clears the key of the row that held it.
        """
        if spec.relation_mode != "reverse_fk" and spec.taken_from is None:
            return None
        return (yield self._ops.holder(model, spec, target))

    def _check_taken(self, model: type, spec: Any, holder: Any, target: Any) -> Steps:
        """Ask to take *target* from *holder*, the other *model* row linked to it."""
        if spec.relation_mode == "reverse_fk":
            yield from self._check_repo(
                self._owner(spec.related_model, target),
                "can_unlink",
                holder,
                spec.name,
                target,
            )
            return
        # The key is on the holder, which is written - or deleted, when the
        # target's relation deletes the row it lets go of - so it has to be a
        # row the caller could unlink by its key.
        owner, visible = yield from self._get(model, self._ops.pk_value(holder))
        if visible is None:
            raise PermissionError(
                f"{self._called(spec.related_model, 'Row')} "
                f"{self._ops.pk_value(target)!r} cannot be taken from the "
                f"{self._called(model, 'row')} it is linked from"
            )
        yield from self._check_repo(
            owner, "can_unlink", target, spec.taken_from, visible
        )
        if spec.orphans_holder:
            yield from self._check_orphaned(owner, visible)

    def _member(
        self, parent: _Parent, pk: Any, model: type
    ) -> Generator[Any, Any, tuple[type, Any]]:
        """The *model* row *pk* names in *parent*'s list, if the caller sees it, and its owner."""
        owner, child = yield from self._get(model, pk)
        if child is not None:
            is_member = yield self._ops.is_member(parent.instance, parent.spec, child)
            if not is_member:
                child = None
        if child is None:
            raise UpsertNotFoundError(
                f"No {self._called(model, 'row')} {pk!r} in {self._relation(parent)}"
            )
        return owner, child

    def _remove_member(
        self, parent: _Parent, operation: str, owner: type, child: Any
    ) -> Steps:
        if operation == "unlink":
            yield from self._check_repo(
                owner, "can_unlink", parent.instance, parent.spec.name, child
            )
            yield self._ops.unlink(parent.instance, parent.spec, child)
        else:
            yield from self._delete_instance(owner, child, parent)

    def _delete_instance(
        self, model: type, instance: Any, parent: _Parent | None = None
    ) -> Steps:
        """Delete *instance* through *model*'s repo, the one that owns it.

        Callers read the row through :meth:`_get`, so that repo has seen it.
        """
        repo = self._repo(model)
        yield from self._check_repo(model, "can_delete", instance)
        yield repo.on_before_delete(instance, self._info)
        if parent is None or parent.spec.relation_mode == "reverse_fk":
            yield self._ops.delete(repo, instance)
        elif parent.spec.deletes_orphans:
            # Let go of first, the row would be deleted past a soft-deleting
            # ``_delete``, so its link rows go only once it is gone.
            yield self._ops.delete(repo, instance)
            yield self._ops.drop_link(parent.instance, parent.spec, instance)
        else:
            # Out of the join table first, so no link row outlives the row.
            yield self._ops.unlink(parent.instance, parent.spec, instance)
            yield self._ops.delete(repo, instance)

    def _delete_replaced(self, model: type, previous: Any) -> Steps:
        """Delete a link's old target, if the caller could delete it by its key.

        The target was read off the row, not through the repo, so it is read
        again through the repo's ``scope_query``. One that hides it, or an
        ``authorize`` that denies its deletion, leaves it unlinked but in place,
        and the caller is told nothing either way.
        """
        owner, visible = yield from self._get(model, self._ops.pk_value(previous))
        if visible is None:
            return
        if (yield from self._denied("delete", model, visible)) is None:
            yield from self._delete_instance(owner, visible)

    def _check_orphaned(self, model: type, previous: Any) -> Steps:
        """Ask to delete a link's old target, which the ORM deletes once let go of.

        The cascade deletes it as the row is saved, whatever ``onReplace``
        says, so it cannot be left in place: re-pointing the link is refused
        unless the caller could delete the target by its key.
        """
        owner, visible = yield from self._get(model, self._ops.pk_value(previous))
        if visible is None:
            # The key is the row's, which the caller may not be able to read.
            raise PermissionError(
                f"The {self._called(model, 'row')} the link held would be deleted "
                f"by re-pointing it, and cannot be deleted"
            )
        yield from self._check_allowed("delete", model, visible)
        yield from self._check_repo(owner, "can_delete", visible)
        yield self._repo(owner).on_before_delete(visible, self._info)

    def _update_by_id(self, payload: Any, cls: type) -> Steps:
        model = cls.__orm_model__  # type: ignore[attr-defined]
        pk = self._pk(payload.id, model)
        _owner, instance = yield from self._get(model, pk)
        if instance is None:
            raise UpsertNotFoundError(f"No {self._called(model, 'row')} {pk!r}")
        yield from self._check_allowed("update", model, instance)
        return (yield from self._update(instance, payload, cls))

    def _link_target(
        self, link: Any, cls: type
    ) -> Generator[Any, Any, tuple[Any, bool]]:
        """The row a single link points at, and whether it existed before."""
        operation, value = _selected_operation(link, cls)
        fields = _declared_fields(cls)
        if operation == "create":
            return (yield from self._create(value, fields["create"], None, {})), False
        if operation == "upsert":
            return (yield from self._upsert(value, fields["upsert"], None))
        return (yield from self._update_by_id(value, fields["update"])), True

    def _same_row(self, previous: Any, target: Any) -> bool:
        return previous is not None and (
            self._ops.pk_value(previous) == self._ops.pk_value(target)
        )

    def _on_replace(self, link: Any, cls: type) -> str:
        sent = getattr(link, "on_replace", strawberry.UNSET)
        if _is_set(sent):
            return getattr(sent, "value", sent)
        return cls.__relation_policy__["default_on_replace"]  # type: ignore[attr-defined]
