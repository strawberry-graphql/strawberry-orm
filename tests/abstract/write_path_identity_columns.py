"""Only the app writes the columns that pick which row, or which model, a row is.

A row's key picks the row a save lands on: its primary key, each column of a
key made of several, and a key that is also a link to another row. Django
saves a row whose composite key is set with an ``UPDATE`` matching the new
values, so an update setting one overwrites the row that key names, which no
repo was asked about, or inserts one past ``can_create``. A discriminator, or
any column a discriminator expression reads, picks the model the row loads
as: written, it makes the row one of a subclass whose repo is never asked.

So no generated input offers them, no projection names them, no hook or
hand-written input sets one on an update, a create cannot pick its row's
model, and a row held by a link that keys it, or picks its model, is neither
moved to another holder nor let go of.

Each backend supplies an ``identity`` fixture, an :class:`IdentityDb` over its
own models, and a ``shape`` fixture naming one of its :class:`Shape` entries.
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
    item,
    member,
)

ME, THEM = 7, 99
SECRET = "their secret plan"

#: What a write refused for its identity columns raises.
REFUSED = (ValueError, UpsertValidationError, PermissionError)


@dataclass
class Held:
    """``model``'s rows held in a list of ``holder`` rows by a link that keys
    them, or picks their model."""

    holder: type
    relation: str
    #: The holder of the caller's row, and one another row could be moved to.
    mine: int
    other: int
    #: Whether the link picks the model, rather than the key, of the rows held.
    picks_model: bool = False


@dataclass
class Shape:
    """One way a model's columns pick its rows, or their model."""

    #: The model the inputs are generated for, and written through.
    model: type
    #: Every attribute picking which row, or which model, a row of ``model`` is.
    identity: set[str]
    #: The columns keying the caller's row, and the row a hidden repo keeps.
    mine: dict[str, Any]
    theirs: dict[str, Any]
    #: Update data moving the caller's row: onto the other row's key, onto a
    #: free one, or into another model.
    moves: list[dict[str, Any]]
    #: Create data taking the other row's key, or picking another model.
    forges: list[dict[str, Any]]
    #: The columns read off every row to tell whether a write moved one.
    columns: tuple[str, ...]
    #: Of ``identity``, the attributes picking the model.
    picks_model: set[str] = field(default_factory=set)
    #: The caller's row's id, if a write can name the row by it.
    own_id: str | None = None
    #: The model whose rows are read back, if not ``model``.
    base: type | None = None
    #: Models whose repo refuses every write: the ones a moved row turns into.
    sealed: tuple[type, ...] = ()
    #: Other models the caller's repo covers.
    owned: tuple[type, ...] = ()
    #: Whether a row of ``sealed[0]``, created through it, is given its
    #: discriminator by the ORM.
    sealed_sets_its_own: bool = False
    #: The relation of ``m.Holder`` holding rows of ``model``.
    relation: str | None = None
    #: The relation of ``m.Pin`` linking one row of ``model``.
    link: str | None = None
    held_by: Held | None = None
    #: What the app's hook gives a new row the client cannot: its key, when
    #: nothing generates one.
    keyed_by_hook: dict[str, Any] = field(default_factory=dict)

    @property
    def read_as(self) -> type:
        return self.base or self.model


@dataclass
class IdentityDb(WriteDb):
    shapes: dict[str, Shape] = field(default_factory=dict)

    def owned(self, query: Any, model: type) -> Any:
        """*query* narrowed to the caller's rows of *model*."""
        raise NotImplementedError

    async def fetch(self, model: type, pk: Any) -> Any:
        raise NotImplementedError

    async def state(self, shape: Shape) -> list[tuple[Any, ...]]:
        """Every row the shape's writes could reach, as what keys and types it."""
        rows = await self.rows(shape.read_as)
        return sorted(
            (
                (
                    *(getattr(row, column) for column in shape.columns),
                    row.title,
                    row.owner,
                    type(row).__name__,
                )
                for row in rows
            ),
            key=repr,
        )

    async def seed(self, shape: Shape) -> None:
        holders = [shape.held_by.holder] if shape.held_by else []
        for model in (self.m.Holder, *holders):
            for pk in (1, 2, 3):
                await self.create(model, id=pk, owner=THEM)
        mine = {"title": "mine", "owner": ME, "holder_id": 1}
        await self.create(shape.model, **shape.mine, **mine)
        theirs = {"title": SECRET, "owner": THEM, "holder_id": 2}
        await self.create(shape.model, **shape.theirs, **theirs)


def own_rows(
    db: IdentityDb,
    model: type,
    *,
    on_create: dict[str, Any] | None = None,
    on_update: dict[str, Any] | None = None,
) -> type:
    """The README's repo: the caller reads, creates and edits its rows only."""

    class OwnRows(AbstractRepo):
        def scope_query(self, query: Any, info: Any) -> Any:
            return db.owned(query, model)

        def on_before_create(self, data: dict[str, Any], info: Any) -> dict[str, Any]:
            return {**data, "owner": ME, **(on_create or {})}

        def can_create(self, data: dict[str, Any], info: Any) -> bool:
            return data.get("owner") == ME

        def on_before_update(
            self, instance: Any, data: dict[str, Any], info: Any
        ) -> dict[str, Any]:
            return {**data, **(on_update or {})}

        def can_update(self, instance: Any, data: dict[str, Any], info: Any) -> bool:
            return instance.owner == ME

    return OwnRows


def sealed_off(db: IdentityDb, model: type) -> type:
    """A repo hiding every row of *model* and refusing every write."""

    class SealedOff(AbstractRepo):
        def scope_query(self, query: Any, info: Any) -> Any:
            return db.only(query, model, [])

        def can_create(self, data: dict[str, Any], info: Any) -> bool:
            return False

        def can_update(self, instance: Any, data: dict[str, Any], info: Any) -> bool:
            return False

        def can_link(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
            return False

        def can_unlink(self, parent: Any, field: str, instance: Any, info: Any) -> bool:
            return False

    return SealedOff


def repos(db: IdentityDb, shape: Shape, **hooks: Any) -> dict[type, type]:
    caller = {shape.model, shape.read_as, *shape.owned}
    return {
        **{model: own_rows(db, model, **hooks) for model in caller},
        **{model: sealed_off(db, model) for model in shape.sealed},
    }


def _name(shape: Shape, what: str) -> str:
    return f"{shape.model.__name__}{what}"


def _hand_written(
    shape: Shape, what: str, values: dict[str, Any], *, id_: bool
) -> type:
    """An input the app wrote by hand, which offers *values*' fields."""
    annotations: dict[str, Any] = {"id": strawberry.ID} if id_ else {}
    annotations["title"] = str | None
    annotations.update({name: type(value) | None for name, value in values.items()})
    namespace = {"__annotations__": annotations}
    namespace.update(dict.fromkeys(set(annotations) - {"id"}, strawberry.UNSET))
    return strawberry.input(type(_name(shape, what), (), namespace))


def _holder_upsert(
    orm: Any, holder: type, relation: str, ops: dict[str, bool], name: str
) -> type:
    return orm.mutations.upsert_node_input(
        model=holder,
        project={
            "_meta": {"update": True, "upsert": {"where": ["id"]}},
            relation: {"_meta": ops},
        },
        name=name,
    )


def _under_holder(upsert: type, relation: str, pk: int, *items: Any) -> Any:
    update = member(upsert, "update")
    return upsert(
        where=member(upsert, "where")(id=str(pk)),
        update=update(**{relation: list(items)}),
    )


def _offered(orm: Any, db: IdentityDb, shape: Shape, kind: str) -> set[str]:
    model, key = shape.model, shape.model.__name__
    if kind == "input":
        return fields_of(orm.input(model, name=_name(shape, "IcInput")))
    if kind == "partial":
        return fields_of(orm.partial(model, name=_name(shape, "IcPartial")))
    if kind == "input_including":
        named = sorted(shape.identity | {"title"})
        return fields_of(orm.input(model, include=named, name=_name(shape, "IcAll")))
    if kind in ("node_create", "node_update"):
        build = getattr(orm.mutations, f"{kind.removeprefix('node_')}_node_input")
        root = build(models=[model], name=_name(shape, f"Ic{kind}"))
        fields = fields_of(member(root, key[:1].lower() + key[1:]))
        return fields - {"id"} if kind == "node_update" else fields
    if kind.startswith("upsert_"):
        upsert = orm.mutations.upsert_node_input(
            model=model,
            project={
                "_meta": {
                    "create": True,
                    "update": True,
                    "upsert": {"where": True, "onMatch": "UPDATE"},
                }
            },
            name=_name(shape, "IcUpsert"),
        )
        fields = fields_of(member(upsert, kind.removeprefix("upsert_")))
        # ``where: {id}`` finds a row by its key, and never names a new one.
        return fields - {"id"} if kind == "upsert_where" else fields
    if kind.startswith("ref_list_"):
        if shape.relation is None:
            pytest.skip(f"no list holds {key} rows")
        ops = {"create": True, "update": True}
        upsert = _holder_upsert(
            orm, db.m.Holder, shape.relation, ops, _name(shape, "IcHolder")
        )
        operation = kind.removeprefix("ref_list_")
        fields = fields_of(member(upsert, "update", shape.relation, operation))
        return fields - {"id"} if operation == "update" else fields
    assert kind in ("link_create", "link_update"), kind
    if shape.link is None:
        pytest.skip(f"no link holds one {key} row")
    upsert = orm.mutations.upsert_node_input(
        model=db.m.Pin,
        project={
            "_meta": {"update": True, "upsert": {"where": ["id"]}},
            shape.link: {
                "_meta": {
                    "create": True,
                    "update": True,
                    "upsert": {"where": ["title"]},
                },
            },
        },
        name=_name(shape, "IcPin"),
    )
    target = member(upsert, "update", shape.link, "upsert")
    return fields_of(member(target, kind.removeprefix("link_")))


class AbstractTestInputsOfferNoIdentityColumn:
    @pytest.mark.parametrize(
        "kind",
        [
            "input",
            "partial",
            "input_including",
            "node_create",
            "node_update",
            "upsert_create",
            "upsert_update",
            "upsert_where",
            "ref_list_create",
            "ref_list_update",
            "link_create",
            "link_update",
        ],
    )
    def test_no_generated_input_offers_one(self, identity, shape, kind):
        current = identity.shapes[shape]
        offered = _offered(identity.new_orm(), identity, current, kind)
        assert not offered & current.identity, (
            f"offers {sorted(offered & current.identity)}"
        )

    def test_exclude_pk_false_never_offers_what_picks_the_model(self, identity, shape):
        """The one way an app asks for a model's keys; never for its type."""
        current = identity.shapes[shape]
        offered = fields_of(
            identity.new_orm().input(
                current.model,
                exclude_pk=False,
                include=sorted(current.identity | {"title"}),
                name=_name(current, "IcWithPk"),
            )
        )
        assert "title" in offered, f"offers only {sorted(offered)}"
        assert not offered & current.picks_model, (
            f"offers {sorted(offered & current.picks_model)}"
        )

    @pytest.mark.parametrize("option", ["where", "create", "update"])
    def test_a_projection_cannot_name_one(self, identity, shape, option):
        """``where: ["id"]`` finds a row by its key; anything else in ``where``
        seeds the create a miss makes."""
        current = identity.shapes[shape]
        orm = identity.new_orm()
        names = current.identity - {"id"} if option == "where" else current.identity
        accepted = []
        for name in sorted(names):
            meta: dict[str, Any] = {"upsert": {"where": ["title"]}}
            if option == "where":
                meta["upsert"] = {"where": [name]}
            else:
                meta[option] = [name]
            try:
                orm.mutations.upsert_node_input(
                    model=current.model,
                    project={"_meta": meta},
                    name=_name(current, f"{option}{name.replace('_', '')}"),
                )
            except ValueError:
                continue
            accepted.append(name)
        assert not accepted, f"accepts {accepted}"


class _Writes:
    async def _seeded(self, identity: IdentityDb, current: Shape) -> list[Any]:
        await identity.seed(current)
        return await identity.state(current)

    async def _unchanged(
        self, identity: IdentityDb, current: Shape, before: list[Any]
    ) -> None:
        after = await identity.state(current)
        assert after == before, (
            f"wrote {sorted(set(after) - set(before), key=repr)} in place of "
            f"{sorted(set(before) - set(after), key=repr)}"
        )

    def _needs_id(self, current: Shape, route: str) -> None:
        if route != "upsert" and current.own_id is None:
            pytest.skip(f"no write names a {current.model.__name__} row by its id")

    def _needs_relation(self, current: Shape, route: str) -> None:
        if route != "upsert" and current.relation is None:
            pytest.skip(f"no list holds {current.model.__name__} rows")


class AbstractTestAnUpdateKeepsItsRowAndModel(_Writes):
    @pytest.mark.parametrize("route", ["upsert", "nested", "ref_list"])
    async def test_an_update_setting_none_is_written(self, identity, shape, route):
        current = identity.shapes[shape]
        self._needs_relation(current, route)
        self._needs_id(current, route)
        await identity.seed(current)
        orm = identity.new_orm(repos=repos(identity, current))
        await self._update(identity, orm, current, route, 0)
        titles = sorted(row[-3] for row in await identity.state(current))
        assert titles == ["edited", SECRET], f"wrote {titles}"

    @pytest.mark.parametrize("route", ["upsert", "nested", "ref_list"])
    async def test_an_update_hook_cannot_set_one(self, identity, shape, route):
        """A repo, or a resolver, that copies a client's value into one."""
        current = identity.shapes[shape]
        self._needs_relation(current, route)
        self._needs_id(current, route)
        before = await self._seeded(identity, current)
        for number, move in enumerate(current.moves):
            orm = identity.new_orm(repos=repos(identity, current, on_update=move))
            with suppress(*REFUSED):
                await self._update(identity, orm, current, route, number)
        await self._unchanged(identity, current, before)

    async def _update(
        self, identity: IdentityDb, orm: Any, current: Shape, route: str, number: int
    ) -> None:
        if route == "upsert":
            upsert = orm.mutations.upsert_node_input(
                model=current.model,
                project={
                    "_meta": {
                        "update": True,
                        "upsert": {"where": ["title"], "onMatch": "UPDATE"},
                    }
                },
                name=_name(current, f"IcMove{number}"),
            )
            fields = {name: member(upsert, name) for name in ("where", "update")}
            payload = upsert(
                where=fields["where"](title="mine"),
                update=fields["update"](title="edited"),
            )
            await identity.upsert(orm, payload)
        elif route == "nested":
            upsert = _holder_upsert(
                orm,
                identity.m.Holder,
                current.relation,
                {"update": True},
                _name(current, f"IcHolderMove{number}"),
            )
            rows = member(upsert, "update", current.relation)
            edit = item(rows, "update", id=current.own_id, title="edited")
            await identity.upsert(orm, _under_holder(upsert, current.relation, 1, edit))
        else:
            edit = _hand_written(current, f"IcEdit{number}", {}, id_=True)
            ref = orm.ref(current.model, update=edit)
            holder = await identity.fetch(identity.m.Holder, 1)
            payload = [ref(update=edit(id=current.own_id, title="edited"))]
            await identity.refs(orm, holder, current.relation, payload)

    async def test_a_hand_written_update_cannot_set_one(self, identity, shape):
        current = identity.shapes[shape]
        self._needs_relation(current, "ref_list")
        self._needs_id(current, "ref_list")
        # Its ``id`` names the row the update is for.
        moves = [move for move in current.moves if "id" not in move]
        if not moves:
            pytest.skip(f"only the id keys {current.model.__name__} rows")
        before = await self._seeded(identity, current)
        orm = identity.new_orm(repos=repos(identity, current))
        for number, move in enumerate(moves):
            edit = _hand_written(current, f"IcStolenEdit{number}", move, id_=True)
            ref = orm.ref(current.model, update=edit)
            holder = await identity.fetch(identity.m.Holder, 1)
            payload = [ref(update=edit(id=current.own_id, title="edited", **move))]
            with suppress(*REFUSED):
                await identity.refs(orm, holder, current.relation, payload)
        await self._unchanged(identity, current, before)


class AbstractTestACreateKeepsItsModel(_Writes):
    @pytest.mark.parametrize("route", ["upsert", "nested", "ref_list"])
    async def test_a_create_setting_none_is_written(self, identity, shape, route):
        current = identity.shapes[shape]
        self._needs_relation(current, route)
        await identity.seed(current)
        orm = identity.new_orm(
            repos=repos(identity, current, on_create=current.keyed_by_hook)
        )
        await self._create(identity, orm, current, route, 0)
        made = [row for row in await identity.state(current) if row[-3] == "forged"]
        assert [row[-2:] for row in made] == [(ME, current.model.__name__)], (
            f"made {made}"
        )

    @pytest.mark.parametrize("route", ["upsert", "nested", "ref_list"])
    async def test_a_create_hook_cannot_pick_the_model_or_take_a_row(
        self, identity, shape, route
    ):
        current = identity.shapes[shape]
        self._needs_relation(current, route)
        before = await self._seeded(identity, current)
        for number, forge in enumerate(current.forges):
            orm = identity.new_orm(repos=repos(identity, current, on_create=forge))
            with suppress(*REFUSED):
                await self._create(identity, orm, current, route, number)
        await self._unchanged(identity, current, before)

    async def _create(
        self, identity: IdentityDb, orm: Any, current: Shape, route: str, number: int
    ) -> None:
        if route == "upsert":
            upsert = orm.mutations.upsert_node_input(
                model=current.model,
                project={
                    "_meta": {
                        "create": True,
                        "upsert": {"where": ["title"], "onMatch": "IGNORE"},
                    }
                },
                name=_name(current, f"IcForge{number}"),
            )
            fields = {name: member(upsert, name) for name in ("where", "create")}
            payload = upsert(
                where=fields["where"](title="forged"),
                create=fields["create"](title="forged"),
            )
            await identity.upsert(orm, payload)
        elif route == "nested":
            upsert = _holder_upsert(
                orm,
                identity.m.Holder,
                current.relation,
                {"create": True},
                _name(current, f"IcHolderForge{number}"),
            )
            rows = member(upsert, "update", current.relation)
            made = item(rows, "create", title="forged")
            await identity.upsert(orm, _under_holder(upsert, current.relation, 1, made))
        else:
            fresh = _hand_written(current, f"IcFresh{number}", {}, id_=False)
            ref = orm.ref(current.model, create=fresh)
            holder = await identity.fetch(identity.m.Holder, 1)
            payload = [ref(create=fresh(title="forged"))]
            await identity.refs(orm, holder, current.relation, payload)

    async def test_a_hand_written_create_cannot_pick_the_model_or_take_a_row(
        self, identity, shape
    ):
        current = identity.shapes[shape]
        self._needs_relation(current, "ref_list")
        before = await self._seeded(identity, current)
        orm = identity.new_orm(repos=repos(identity, current))
        for number, forge in enumerate(current.forges):
            stolen = _hand_written(current, f"IcStolen{number}", forge, id_=False)
            ref = orm.ref(current.model, create=stolen)
            holder = await identity.fetch(identity.m.Holder, 1)
            payload = [ref(create=stolen(title="forged", **forge))]
            with suppress(*REFUSED):
                await identity.refs(orm, holder, current.relation, payload)
        await self._unchanged(identity, current, before)

    async def test_a_row_of_a_subclass_is_still_created_through_it(
        self, identity, shape
    ):
        """The model a write is asked for still sets the row's type."""
        current = identity.shapes[shape]
        if not current.sealed_sets_its_own:
            pytest.skip(f"no subclass of {current.model.__name__} sets its own type")
        await identity.seed(current)
        subclass = current.sealed[0]
        orm = identity.new_orm(
            repos={model: own_rows(identity, model) for model in current.sealed}
        )
        upsert = orm.mutations.upsert_node_input(
            model=subclass,
            project={
                "_meta": {
                    "create": True,
                    "upsert": {"where": ["title"], "onMatch": "IGNORE"},
                }
            },
            name=f"{subclass.__name__}IcMade",
        )
        fields = {name: member(upsert, name) for name in ("where", "create")}
        await identity.upsert(
            orm,
            upsert(
                where=fields["where"](title="made"),
                create=fields["create"](title="made"),
            ),
        )
        made = [row for row in await identity.state(current) if row[-3] == "made"]
        assert [row[-1] for row in made] == [subclass.__name__], f"made {made}"


class AbstractTestALinkKeepsItsRowAndModel(_Writes):
    """A row whose link to a holder keys it, or picks its model, stays with
    the holder it was created under."""

    def _held(self, current: Shape, route: str) -> Held:
        if current.held_by is None:
            pytest.skip(f"no link keys {current.model.__name__} rows")
        self._needs_id(current, route)
        return current.held_by

    async def _write_under(
        self,
        identity: IdentityDb,
        orm: Any,
        current: Shape,
        route: str,
        pk: int,
        operation: str,
        **values: Any,
    ) -> None:
        held = current.held_by
        if route == "nested":
            upsert = _holder_upsert(
                orm,
                held.holder,
                held.relation,
                {operation: True},
                _name(current, f"IcHeld{operation.title()}"),
            )
            rows = member(upsert, "update", held.relation)
            await identity.upsert(
                orm,
                _under_holder(
                    upsert, held.relation, pk, item(rows, operation, **values)
                ),
            )
            return
        if operation == "unlink":
            ref = orm.ref(current.model, unlink=True)
            payload = [item(ref, "unlink", **values)]
        else:
            written = _hand_written(
                current, f"IcHeld{operation.title()}", {}, id_=operation == "update"
            )
            ref = orm.ref(current.model, **{operation: written})
            payload = [ref(**{operation: written(**values)})]
        holder = await identity.fetch(held.holder, pk)
        await identity.refs(orm, holder, held.relation, payload)

    @pytest.mark.parametrize("route", ["nested", "ref_list"])
    async def test_a_row_cannot_move_to_another_holder(self, identity, shape, route):
        current = identity.shapes[shape]
        held = self._held(current, route)
        before = await self._seeded(identity, current)
        orm = identity.new_orm(repos=repos(identity, current))
        with suppress(*REFUSED):
            await self._write_under(
                identity, orm, current, route, held.other, "update", id=current.own_id
            )
        await self._unchanged(identity, current, before)

    @pytest.mark.parametrize("route", ["nested", "ref_list"])
    async def test_a_row_cannot_leave_its_holder(self, identity, shape, route):
        current = identity.shapes[shape]
        held = self._held(current, route)
        before = await self._seeded(identity, current)
        orm = identity.new_orm(repos=repos(identity, current))
        with suppress(*REFUSED):
            await self._write_under(
                identity, orm, current, route, held.mine, "unlink", id=current.own_id
            )
        await self._unchanged(identity, current, before)

    @pytest.mark.parametrize("route", ["nested", "ref_list"])
    async def test_a_create_under_a_holder_cannot_pick_the_model(
        self, identity, shape, route
    ):
        current = identity.shapes[shape]
        held = self._held(current, route)
        if not held.picks_model:
            pytest.skip(f"the link keys {current.model.__name__} rows")
        before = await self._seeded(identity, current)
        orm = identity.new_orm(repos=repos(identity, current))
        with suppress(*REFUSED):
            await self._write_under(
                identity, orm, current, route, held.other, "create", title="forged"
            )
        await self._unchanged(identity, current, before)

    @pytest.mark.parametrize("route", ["nested", "ref_list"])
    async def test_a_row_keyed_by_its_holder_is_still_created_under_it(
        self, identity, shape, route
    ):
        current = identity.shapes[shape]
        held = self._held(current, route)
        if held.picks_model:
            pytest.skip(f"the link picks the model of {current.model.__name__} rows")
        await identity.seed(current)
        orm = identity.new_orm(
            repos=repos(identity, current, on_create=current.keyed_by_hook)
        )
        await self._write_under(
            identity, orm, current, route, held.other, "create", title="fresh"
        )
        made = [row for row in await identity.state(current) if row[-3] == "fresh"]
        assert [row[-2:] for row in made] == [(ME, current.model.__name__)], (
            f"made {made}"
        )
