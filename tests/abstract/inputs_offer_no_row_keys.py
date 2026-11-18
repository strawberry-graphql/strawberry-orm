"""Generated inputs never name an existing row, and a create cannot reach one.

A column that identifies a row - a primary key, a key inherited from a parent
model, a parent link, a one-to-one key - picks the row a write lands on. Django
saves a multi-table child's parents with an ``UPDATE`` when their key is set,
so a create carrying one rewrites a parent row no repo was asked about, and an
update setting one moves its row onto another's key. So no generated input
offers them, ``include=`` does not name them back, and a create handed one
anyway - by a hand-written input, or a hook copying fields into it - inserts
or is refused.

Each backend supplies a ``keyed`` fixture, a :class:`KeyedDb` over its own
models, and a ``shape`` fixture naming one of its :class:`Shape` entries.
"""

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pytest
import strawberry

from strawberry_orm import AbstractRepo, UpsertValidationError
from tests.abstract.write_path_authorization import (
    WriteDb,
    fields_of,
    member,
)

ME, THEM = 7, 99
SECRET = "their secret plan"


@dataclass
class Shape:
    """One way a model reaches its rows' keys."""

    #: The model the inputs are generated for.
    model: type
    #: Every attribute naming one of its rows, or a parent's row.
    keys: set[str]
    #: The keys a hand-written create can carry as values.
    stolen: tuple[str, ...]
    #: The model whose rows the stolen keys name, and its key's attribute.
    victim: type
    victim_key: str = "id"
    #: The relation of ``m.Binder`` holding rows of ``model``, if one can.
    relation: str | None = None
    #: The relation of ``m.Pin`` linking one row of ``model``.
    link: str | None = None
    #: Values every new row of ``model`` and of ``victim`` needs.
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class KeyedDb(WriteDb):
    shapes: dict[str, Shape] = field(default_factory=dict)

    def owned(self, query: Any, model: type) -> Any:
        """*query* narrowed to the caller's rows of *model*."""
        raise NotImplementedError


def own_rows(
    keyed: KeyedDb,
    model: type,
    *,
    on_update: dict[str, Any] | None = None,
    **extra: Any,
) -> type:
    """The README's repo: the caller reads, creates and edits its rows only."""

    class OwnRows(AbstractRepo):
        def scope_query(self, query: Any, info: Any) -> Any:
            return keyed.owned(query, model)

        def on_before_create(self, data: dict[str, Any], info: Any) -> dict[str, Any]:
            return {**data, "owner": ME, **extra}

        def can_create(self, data: dict[str, Any], info: Any) -> bool:
            return data.get("owner") == ME

        def on_before_update(
            self, instance: Any, data: dict[str, Any], info: Any
        ) -> dict[str, Any]:
            return {**data, **(on_update or {})}

        def can_update(self, instance: Any, data: dict[str, Any], info: Any) -> bool:
            return instance.owner == ME

    return OwnRows


def _models(shape: Shape) -> set[type]:
    return {shape.model, shape.victim}


def _offered(orm: Any, keyed: KeyedDb, shape: Shape, kind: str) -> set[str]:
    model, key = shape.model, shape.model.__name__
    if kind == "input":
        return fields_of(orm.input(model, name=f"{key}KeyInput"))
    if kind == "partial":
        return fields_of(orm.partial(model, name=f"{key}KeyPartial"))
    if kind == "input_including_keys":
        named = sorted(shape.keys | {"title"})
        return fields_of(orm.input(model, include=named, name=f"{key}KeyIncluded"))
    if kind in ("node_create", "node_update"):
        build = getattr(orm.mutations, f"{kind.removeprefix('node_')}_node_input")
        root = build(models=[model], name=f"{key}Key{kind}")
        fields = fields_of(member(root, key[:1].lower() + key[1:]))
        return fields - {"id"} if kind == "node_update" else fields
    if kind in ("upsert_create", "upsert_update"):
        upsert = orm.mutations.upsert_node_input(
            model=model,
            project={
                "_meta": {
                    "create": True,
                    "update": True,
                    "upsert": {"where": ["title"], "onMatch": "UPDATE"},
                }
            },
            name=f"{key}KeyUpsert",
        )
        return fields_of(member(upsert, kind.removeprefix("upsert_")))
    if kind == "ref_list_create":
        if shape.relation is None:
            pytest.skip(f"no relation holds {key} rows apart from its base's")
        root = orm.mutations.create_node_input(
            models=[keyed.m.Binder], name=f"{key}KeyBinder"
        )
        return fields_of(member(root, "wkBinder", shape.relation, "create"))
    assert kind == "link_create"
    root = orm.mutations.create_node_input(models=[keyed.m.Pin], name=f"{key}KeyPin")
    return fields_of(member(root, "wkPin", shape.link, "create"))


def _stolen_create(model: type, key: str) -> type:
    """A create input the app wrote by hand, which offers *key*."""
    annotations = {"title": str | None, key: int | None}
    namespace = {"__annotations__": annotations, "title": None, key: None}
    return strawberry.input(type(f"{model.__name__}Stolen{key.title()}", (), namespace))


def _stolen_update(model: type, key: str) -> type:
    """An update input the app wrote by hand, which offers *key* by its id."""
    annotations = {"id": int, "title": str | None, key: int | None}
    namespace = {"__annotations__": annotations, "title": None, key: None}
    name = f"{model.__name__}StolenUpdate{key.title()}"
    return strawberry.input(type(name, (), namespace))


class AbstractTestInputsOfferNoRowKeys:
    @pytest.mark.parametrize(
        "kind",
        [
            "input",
            "partial",
            "input_including_keys",
            "node_create",
            "node_update",
            "upsert_create",
            "upsert_update",
            "ref_list_create",
            "link_create",
        ],
    )
    def test_no_generated_input_offers_a_row_key(self, keyed, shape, kind):
        current = keyed.shapes[shape]
        offered = _offered(keyed.new_orm(), keyed, current, kind) & current.keys
        assert not offered, f"offers {sorted(offered)}"

    def test_exclude_pk_false_still_offers_the_model_key(self, keyed, shape):
        """The one way an app asks for the key, as for any model; a key that
        is also a link's column takes ``include=`` as well."""
        current = keyed.shapes[shape]
        orm = keyed.new_orm()
        offered = fields_of(
            orm.input(
                current.model,
                exclude_pk=False,
                include=[current.victim_key, "title"],
                name=f"{current.model.__name__}KeyWithPk",
            )
        )
        assert current.victim_key in offered, f"offers only {sorted(offered)}"

    @pytest.mark.parametrize("option", ["where", "create", "update"])
    def test_a_projection_cannot_name_a_row_key(self, keyed, shape, option):
        """``where: ["id"]`` finds a row by its key; any other key in ``where``
        seeds the create a miss makes."""
        current = keyed.shapes[shape]
        orm = keyed.new_orm()
        keys = current.keys - {"id"} if option == "where" else current.keys
        accepted = []
        for key in sorted(keys):
            meta: dict[str, Any] = {"upsert": {"where": ["title"]}}
            if option == "where":
                meta["upsert"] = {"where": [key]}
            else:
                meta[option] = [key]
            try:
                orm.mutations.upsert_node_input(
                    model=current.model,
                    project={"_meta": meta},
                    name=f"{current.model.__name__}{option}{key.replace('_', '')}",
                )
            except ValueError:
                continue
            accepted.append(key)
        assert not accepted, f"accepts {accepted}"


class _Victims:
    async def _victims(self, keyed: KeyedDb, current: Shape) -> list[Any]:
        made = []
        for pk in range(1, len(current.stolen) + 1):
            values = {current.victim_key: pk, "title": SECRET, "owner": THEM}
            made.append(await keyed.create(current.victim, **current.values, **values))
        return made

    async def _taken(self, keyed: KeyedDb, current: Shape) -> list[str]:
        """The stolen keys whose victim row no longer reads as it was made."""
        rows = {
            getattr(row, current.victim_key): row
            for row in await keyed.rows(current.victim)
        }
        return [
            key
            for pk, key in enumerate(current.stolen, start=1)
            if (rows[pk].title, rows[pk].owner) != (SECRET, THEM)
        ]


class AbstractTestACreateOnlyInserts(_Victims):
    async def test_a_create_naming_no_key_inserts_a_row(self, keyed, shape):
        """A key the app's hook picks for a new row is still written."""
        current = keyed.shapes[shape]
        if current.relation is None:
            pytest.skip(f"no relation holds {current.model.__name__} rows")
        await self._victims(keyed, current)
        binder = await keyed.create(keyed.m.Binder, owner=ME)
        picked = {} if current.victim_key == "id" else {current.victim_key: 50}
        orm = keyed.new_orm(
            repos={
                model: own_rows(keyed, model, **picked) for model in _models(current)
            }
        )
        fresh = strawberry.input(
            type(
                f"{current.model.__name__}Fresh",
                (),
                {"__annotations__": {"title": str}},
            )
        )
        ref = orm.ref(current.model, create=fresh)
        await keyed.refs(
            orm, binder, current.relation, [ref(create=fresh(title="new"))]
        )
        made = [row for row in await keyed.rows(current.model) if row.title == "new"]
        assert [row.owner for row in made] == [ME]
        taken = await self._taken(keyed, current)
        assert not taken, f"took the row named by {taken}"

    async def test_a_hand_written_create_cannot_take_a_row_by_its_key(
        self, keyed, shape
    ):
        current = keyed.shapes[shape]
        if current.relation is None:
            pytest.skip(f"no relation holds {current.model.__name__} rows")
        await self._victims(keyed, current)
        binder = await keyed.create(keyed.m.Binder, owner=ME)
        orm = keyed.new_orm(
            repos={model: own_rows(keyed, model) for model in _models(current)}
        )
        for pk, key in enumerate(current.stolen, start=1):
            stolen = _stolen_create(current.model, key)
            ref = orm.ref(current.model, create=stolen)
            payload = [ref(create=stolen(title="pwned", **{key: pk}))]
            with suppress(ValueError, UpsertValidationError):
                await keyed.refs(orm, binder, current.relation, payload)
        taken = await self._taken(keyed, current)
        assert not taken, f"took the row named by {taken}"

    async def test_a_create_hook_cannot_take_a_row_by_its_key(self, keyed, shape):
        """A repo, or a resolver, that copies a client's value into the key."""
        current = keyed.shapes[shape]
        await self._victims(keyed, current)
        for pk, key in enumerate(current.stolen, start=1):
            orm = keyed.new_orm(
                repos={
                    model: own_rows(keyed, model, **{key: pk})
                    for model in _models(current)
                }
            )
            upsert = orm.mutations.upsert_node_input(
                model=current.model,
                project={
                    "_meta": {
                        "create": True,
                        "upsert": {"where": ["title"], "onMatch": "IGNORE"},
                    }
                },
                name=f"{current.model.__name__}ByTitle{pk}",
            )
            fields = {name: member(upsert, name) for name in ("where", "create")}
            payload = upsert(
                where=fields["where"](title="pwned"),
                create=fields["create"](title="pwned"),
            )
            with suppress(ValueError, UpsertValidationError):
                await keyed.upsert(orm, payload)
        taken = await self._taken(keyed, current)
        assert not taken, f"took the row named by {taken}"


class AbstractTestAnUpdateKeepsItsRow(_Victims):
    """An update setting a key saves onto the row that key names."""

    async def _own(self, keyed: KeyedDb, current: Shape) -> Any:
        """The caller's row of ``model``, keyed apart from every victim."""
        values = {current.victim_key: len(current.stolen) + 1, "owner": ME}
        return await keyed.create(
            current.model, **current.values, **values, title="mine"
        )

    async def test_a_hand_written_update_cannot_move_a_row_onto_another_key(
        self, keyed, shape
    ):
        current = keyed.shapes[shape]
        stolen = [key for key in current.stolen if key != "id"]
        if current.relation is None or not stolen:
            pytest.skip(f"no update of {current.model.__name__} carries a key")
        await self._victims(keyed, current)
        own = len(current.stolen) + 1
        await self._own(keyed, current)
        binder = await keyed.create(keyed.m.Binder, owner=ME)
        orm = keyed.new_orm(
            repos={model: own_rows(keyed, model) for model in _models(current)}
        )
        for key in stolen:
            pk = current.stolen.index(key) + 1
            update = _stolen_update(current.model, key)
            ref = orm.ref(current.model, update=update)
            payload = [ref(update=update(id=own, title="pwned", **{key: pk}))]
            with suppress(ValueError, UpsertValidationError):
                await keyed.refs(orm, binder, current.relation, payload)
        taken = await self._taken(keyed, current)
        assert not taken, f"took the row named by {taken}"

    async def test_an_update_hook_cannot_move_a_row_onto_another_key(
        self, keyed, shape
    ):
        current = keyed.shapes[shape]
        await self._victims(keyed, current)
        await self._own(keyed, current)
        for pk, key in enumerate(current.stolen, start=1):
            orm = keyed.new_orm(
                repos={
                    model: own_rows(keyed, model, on_update={key: pk})
                    for model in _models(current)
                }
            )
            upsert = orm.mutations.upsert_node_input(
                model=current.model,
                project={
                    "_meta": {
                        "update": True,
                        "upsert": {"where": ["title"], "onMatch": "UPDATE"},
                    }
                },
                name=f"{current.model.__name__}Rekey{pk}",
            )
            fields = {name: member(upsert, name) for name in ("where", "update")}
            payload = upsert(
                where=fields["where"](title="mine"),
                update=fields["update"](title="pwned"),
            )
            with suppress(ValueError, UpsertValidationError):
                await keyed.upsert(orm, payload)
        taken = await self._taken(keyed, current)
        assert not taken, f"took the row named by {taken}"


class AbstractTestALinkKeepsItsRow:
    """``m.Slot`` rows are keyed by their link to an ``m.Binder`` row, held in
    its ``slots`` list: moved to another binder, a slot takes the key of the
    slot already there."""

    async def test_a_row_keyed_by_its_link_cannot_move_onto_another(self, keyed):
        slot = keyed.m.Slot
        await keyed.create(slot, binder_id=1, title=SECRET, owner=THEM)
        await keyed.create(slot, binder_id=2, title="mine", owner=ME)
        binder = next(row for row in await keyed.rows(keyed.m.Binder) if row.id == 1)
        orm = keyed.new_orm(repos={slot: own_rows(keyed, slot)})
        move = strawberry.input(
            type("WkSlotMove", (), {"__annotations__": {"id": int}})
        )
        ref = orm.ref(slot, update=move)
        with suppress(ValueError, UpsertValidationError):
            await keyed.refs(orm, binder, "slots", [ref(update=move(id=2))])
        rows = {row.binder_id: (row.title, row.owner) for row in await keyed.rows(slot)}
        assert rows[1] == (SECRET, THEM), f"took the row keyed by 1: {rows[1]}"
