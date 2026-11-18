"""A projection offers only the operations it names, through every arm of every input.

``_meta`` naming none of ``create``, ``update``, ``upsert``, ``unlink`` and
``delete`` offers ``create`` and ``update``; naming any offers only those. An
operation a projection leaves out must not come back through another arm: an
``upsert``'s ``create`` or ``update``, a list item's arm, a root input, or a
default filling in every field.

Each shape below is crossed with each place a projection shapes an input: the
root of ``upsert_node_input()``, ``create_node_input()`` and
``update_node_input()``, a single link (``Post.author``), a many-to-many list
(``Post.tags``) and a reverse foreign key list (``Post.comments``). Each cell
checks what the generated input offers, and that a payload sending an arm the
projection leaves out anyway is refused before anything is written.

Each backend binds :class:`AbstractTestProjectionOperations` with the
``writes`` fixture of ``test_write_path_authorization``.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from strawberry_orm import UpsertValidationError
from strawberry_orm.mutations import RelationRemovalPolicy
from tests.abstract.write_path_authorization import Blog, fields_of, ids, member

#: Stands for the one field of the model a shape lists, and matches on.
F = "<field>"
#: A field list standing for every field the model offers.
ALL = None
OPERATIONS = ("create", "update", "upsert", "unlink", "delete")


@dataclass(frozen=True)
class Shape:
    meta: dict[str, Any]
    #: The operations an input it shapes offers.
    offers: frozenset[str]
    #: The members its ``upsert`` arm offers beside ``where``.
    members: frozenset[str] = frozenset()
    #: The fields each of ``create`` and ``update`` offers; ``ALL`` for every one.
    scalars: dict[str, Any] = field(default_factory=dict)
    on_replace: bool = False


SHAPES = {
    "implicit": Shape(
        {}, frozenset({"create", "update"}), scalars={"create": ALL, "update": ALL}
    ),
    "immutable": Shape(
        {"immutable": True}, frozenset({"create"}), scalars={"create": ALL}
    ),
    "no create": Shape(
        {"update": [F], "upsert": {"where": [F]}},
        frozenset({"update", "upsert"}),
        frozenset({"update"}),
        {"update": [F]},
    ),
    "no update": Shape(
        {"create": [F], "upsert": {"where": [F]}},
        frozenset({"create", "upsert"}),
        frozenset({"create"}),
        {"create": [F]},
    ),
    "upsert alone": Shape({"upsert": {"where": [F]}}, frozenset({"upsert"})),
    "immutable upsert": Shape(
        {"immutable": True, "upsert": {"where": [F]}}, frozenset({"upsert"})
    ),
    "ignore": Shape(
        {"update": [F], "upsert": {"where": [F], "onMatch": "IGNORE"}},
        frozenset({"update", "upsert"}),
        scalars={"update": [F]},
    ),
    "append": Shape(
        {"update": [F], "upsert": {"where": [F], "onMatch": "APPEND"}},
        frozenset({"update", "upsert"}),
        scalars={"update": [F]},
    ),
    "empty lists": Shape(
        {"create": [], "update": [], "upsert": {"where": [F]}},
        frozenset({"create", "update", "upsert"}),
        frozenset({"create", "update"}),
        {"create": [], "update": []},
    ),
    "no upsert": Shape(
        {"create": [F], "update": [F]},
        frozenset({"create", "update"}),
        scalars={"create": [F], "update": [F]},
    ),
    "unlink and delete": Shape(
        {"update": [F], "unlink": True, "delete": True},
        frozenset({"update", "unlink", "delete"}),
        scalars={"update": [F]},
    ),
    "on replace": Shape(
        {"create": [F], "onReplace": ["DISCONNECT", "DELETE"]},
        frozenset({"create"}),
        scalars={"create": [F]},
        on_replace=True,
    ),
    "everything": Shape(
        {
            "create": True,
            "update": True,
            "upsert": {"where": [F]},
            "unlink": True,
            "delete": True,
        },
        frozenset(OPERATIONS),
        frozenset({"create", "update"}),
        {"create": ALL, "update": ALL},
    ),
}

ROOTS = ("upsert root", "create root", "update root")
RELATIONS = {"author": "single", "tags": "many", "comments": "many"}
LOCATIONS = (*ROOTS, *RELATIONS)
#: The model each location's projection shapes, and the field its shapes list.
MODELS = {"author": "User", "tags": "Tag", "comments": "Comment"}
FIELDS = {"Post": "title", "User": "name", "Tag": "name", "Comment": "body"}
#: The rows a write through each location may name; a comment's is on Hello.
EXISTING = {"Post": "Hello", "User": "Bob", "Tag": "rust", "Comment": "mine"}
NEW = {"Post": "Scoop", "User": "Mallory", "Tag": "forged", "Comment": "forged"}
#: The Post projection a relation location is nested in.
POST_META = {"update": ["title"], "upsert": {"where": ["title"]}}


def model_name(location: str) -> str:
    return MODELS.get(location, "Post")


def refused_at(location: str, shape: Shape) -> bool:
    """Whether building *shape* at *location* is a configuration error."""
    if RELATIONS.get(location) == "single":
        return bool(shape.offers & {"unlink", "delete"})
    required = {"upsert root": "upsert", "create root": "create"}
    return location in ROOTS and required.get(location, "update") not in shape.offers


def filled(value: Any, name: str) -> Any:
    if value == F:
        return name
    if isinstance(value, list):
        return [filled(item, name) for item in value]
    if isinstance(value, dict):
        return {key: filled(item, name) for key, item in value.items()}
    return value


def target(location: str, meta: dict[str, Any]) -> dict[str, Any]:
    """*meta* as the projection of *location*'s model; posts and comments link an author."""
    name = model_name(location)
    project: dict[str, Any] = {"_meta": filled(meta, FIELDS[name])}
    if name in ("Post", "Comment"):
        project["author"] = {}
    return project


def build(orm: Any, m: Any, location: str, meta: dict[str, Any]) -> type:
    project = target(location, meta)
    if location == "create root":
        return orm.mutations.create_node_input(
            models=[m.Post], project={"post": project}
        )
    if location == "update root":
        return orm.mutations.update_node_input(
            models=[m.Post], project={"post": project}
        )
    if location == "upsert root":
        return orm.mutations.upsert_node_input(model=m.Post, project=project)
    return orm.mutations.upsert_node_input(
        model=m.Post, project={"_meta": POST_META, location: project}
    )


def scalars(input_type: type) -> set[str]:
    return fields_of(input_type) - set(input_type.__orm_relations__) - {"id"}


def offered_member(input_type: type, name: str) -> type | None:
    return member(input_type, name) if name in fields_of(input_type) else None


def generation_cells() -> list[Any]:
    return [
        pytest.param(location, shape, id=f"{location}-{shape}")
        for location in LOCATIONS
        for shape in SHAPES
    ]


def arms(location: str, shape: Shape) -> list[str]:
    """Every arm at *location* that *shape* leaves out, which a payload can still send."""
    kind = RELATIONS.get(location)
    if kind is None:
        candidates = ["upsert.create", "upsert.update"]
    else:
        candidates = ["create", "update", "upsert", "upsert.create", "upsert.update"]
        candidates += ["onReplace"] if kind == "single" else ["unlink", "delete"]
    left_out = []
    for arm in candidates:
        if arm.startswith("upsert."):
            if "upsert" in shape.offers and arm[7:] not in shape.members:
                left_out.append(arm)
        elif arm == "onReplace":
            if not shape.on_replace:
                left_out.append(arm)
        elif arm not in shape.offers:
            left_out.append(arm)
    return left_out


def runtime_cells() -> list[Any]:
    return [
        pytest.param(location, name, arm, id=f"{location}-{name}-{arm}")
        for location in ("upsert root", *RELATIONS)
        for name, shape in SHAPES.items()
        if not refused_at(location, shape)
        for arm in arms(location, shape)
    ]


async def state(writes: Any, blog: Any) -> tuple[Any, ...]:
    m = writes.m
    return (
        [(row.name, row.email) for row in await writes.rows(m.User)],
        [(row.title, row.body, row.author_id) for row in await writes.rows(m.Post)],
        [row.name for row in await writes.rows(m.Tag)],
        [(row.body, row.post_id) for row in await writes.rows(m.Comment)],
        await ids(writes, blog.hello, "tags"),
        await ids(writes, blog.hello, "comments"),
    )


class AbstractTestProjectionOperations(Blog):
    @pytest.mark.parametrize(("location", "shape"), generation_cells())
    async def test_an_input_offers_only_what_its_projection_names(
        self, writes, location, shape
    ):
        expected = SHAPES[shape]
        orm = writes.new_orm()
        name = model_name(location)
        if refused_at(location, expected):
            with pytest.raises(ValueError):
                build(orm, writes.m, location, expected.meta)
            return
        built = build(orm, writes.m, location, expected.meta)
        reached: dict[str, list[type]] = {"create": [], "update": []}
        upsert: type | None = built
        if location in ("create root", "update root"):
            reached[location.split()[0]].append(member(built, "post"))
            upsert = None
        elif location in RELATIONS:
            container = member(built, "update", location)
            offered = fields_of(container) & set(OPERATIONS)
            assert offered == expected.offers, (
                f"{location} offers {sorted(offered)}, not {sorted(expected.offers)}"
            )
            on_replace = "on_replace" in fields_of(container)
            single = RELATIONS[location] == "single"
            assert on_replace == (expected.on_replace and single), (
                f"{location} offers onReplace: {on_replace}"
            )
            for operation in ("create", "update"):
                arm = offered_member(container, operation)
                if arm is not None:
                    reached[operation].append(arm)
            upsert = offered_member(container, "upsert")
        if upsert is not None:
            members = fields_of(upsert) & {"create", "update"}
            assert members == expected.members, (
                f"the {location} upsert offers {sorted(members)}, not "
                f"{sorted(expected.members)}"
            )
            for operation in members:
                reached[operation].append(member(upsert, operation))
        for operation, types in reached.items():
            listed = expected.scalars.get(operation, [])
            for input_type in types:
                written = scalars(input_type)
                if listed is ALL:
                    assert FIELDS[name] in written, (
                        f"{input_type.__name__} offers only {sorted(written)}"
                    )
                else:
                    allowed = set(filled(listed, FIELDS[name]))
                    assert written == allowed, (
                        f"{input_type.__name__} offers {sorted(written)}, which "
                        f"the projection limits to {sorted(allowed)}"
                    )

    @pytest.mark.parametrize("location", LOCATIONS)
    @pytest.mark.parametrize("key", OPERATIONS)
    async def test_false_leaves_no_operation_to_fall_back_on(
        self, writes, location, key
    ):
        """``False`` is refused, rather than read as a key left out."""
        with pytest.raises(ValueError, match=f"_meta.{key} for"):
            build(writes.new_orm(), writes.m, location, {key: False})

    @pytest.mark.parametrize(("location", "shape", "arm"), runtime_cells())
    async def test_a_payload_sending_an_arm_its_projection_leaves_out_is_refused(
        self, writes, blog, location, shape, arm
    ):
        orm = writes.new_orm()
        m = writes.m
        upsert = build(orm, m, location, SHAPES[shape].meta)
        payload = self.payload(m, upsert, location, arm)
        before = await state(writes, blog)
        with pytest.raises(UpsertValidationError):
            await writes.upsert(orm, payload)
        after = await state(writes, blog)
        assert after == before, f"{arm} at {location} wrote {after} over {before}"

    @classmethod
    def payload(cls, m: Any, upsert: type, location: str, arm: str) -> Any:
        if location == "upsert root":
            return cls.upsert_arm(upsert, "Post", arm[7:])
        container = member(upsert, "update", location)
        item = container()
        if arm == "onReplace":
            operation = next(
                op
                for op in ("update", "upsert", "create")
                if op in fields_of(container)
            )
            cls.send(item, container, operation, model_name(location))
            item.on_replace = RelationRemovalPolicy.DELETE
        elif arm.startswith("upsert."):
            arm_type = member(container, "upsert")
            item.upsert = cls.upsert_arm(arm_type, model_name(location), arm[7:])
        else:
            cls.send(item, container, arm, model_name(location))
        sent = item if RELATIONS[location] == "single" else [item]
        return upsert(
            where=member(upsert, "where")(title="Hello"),
            update=member(upsert, "update")(**{location: sent}),
        )

    @classmethod
    def send(cls, item: Any, container: type, operation: str, name: str) -> None:
        """Set *operation* on *item*, whether or not its type offers it."""
        if operation == "create":
            value = cls.create(offered_member(container, "create"), name)
        elif operation == "upsert":
            arm_type = offered_member(container, "upsert")
            where = {FIELDS[name]: EXISTING[name]}
            value = (
                SimpleNamespace(where=SimpleNamespace(**where))
                if arm_type is None
                else arm_type(where=member(arm_type, "where")(**where))
            )
        else:
            arm_type = offered_member(container, operation)
            value = (SimpleNamespace if arm_type is None else arm_type)(id="1")
        setattr(item, operation, value)

    @classmethod
    def upsert_arm(cls, upsert: type, name: str, sent: str) -> Any:
        """An upsert sending *sent*, ``create`` on a miss or ``update`` on a match."""
        value = NEW[name] if sent == "create" else EXISTING[name]
        arm = upsert(where=member(upsert, "where")(**{FIELDS[name]: value}))
        offered = offered_member(upsert, sent)
        if sent == "create":
            arm.create = cls.create(offered, name)
        else:
            values = {FIELDS[name]: "changed"}
            arm.update = (SimpleNamespace if offered is None else offered)(**values)
        return arm

    @staticmethod
    def create(create: type | None, name: str) -> Any:
        """A new *name* row, through *create* when the input offers it."""
        values: dict[str, Any] = {FIELDS[name]: NEW[name]}
        if name == "User":
            values["email"] = "mallory@example.com"
        if name == "Post":
            values["body"] = "B"
        if create is None:
            return SimpleNamespace(**values)
        if name in ("Post", "Comment"):
            link = member(create, "author")
            values["author"] = link(update=member(link, "update")(id="1"))
        return create(**values)
