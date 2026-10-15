"""Running upserts: ``orm.apply_upsert()`` and the ``upsert`` arm of ``apply_ref_list``.

Each backend supplies a ``db`` fixture: an :class:`UpsertDb` over its own
models, with the schema below built by its own ORM. Most tests post a GraphQL
mutation whose resolver hands its input to ``orm.apply_upsert()``; the rest
build a payload in Python to show the runtime enforces what the schema would.
"""

import datetime
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay
from strawberry.relay import GlobalID

from strawberry_orm import (
    AbstractRepo,
    UpsertNotFoundError,
    UpsertValidationError,
    make_ref_type,
)
from strawberry_orm.types import auto
from strawberry_orm.upserts import _declared_fields

# -- Insert-only projections ---------------------------------------------------

EVENT = {
    "_meta": {
        "name": "Event",
        "immutable": True,
        "create": True,
        "upsert": {"where": ["kind", "occurred_on"]},
    }
}
GROUP = {
    "_meta": {
        "name": "Group",
        "immutable": True,
        "create": True,
        "upsert": {"where": ["name"]},
    }
}
PERSON_REF = {
    "_meta": {
        "name": "PersonRef",
        "immutable": True,
        "create": ["ssn", "dob", "name"],
        "upsert": {"where": ["id", "ssn", "dob"], "optional": True},
    }
}
ADDRESS = {
    "_meta": {
        "name": "Address",
        "immutable": True,
        "create": ["line1"],
        "upsert": {"where": ["line1"]},
    },
    "person": PERSON_REF,
}
PERSON = {
    "_meta": {
        "name": "Person",
        "immutable": True,
        "create": ["ssn", "dob", "name"],
        "upsert": {"where": ["ssn", "dob"]},
    },
    "events": EVENT,
    "groups": GROUP,
    "addresses": ADDRESS,
}
QUIET_PERSON = {
    "_meta": {
        "name": "QuietPerson",
        "create": ["ssn", "dob", "name"],
        "upsert": {"where": ["ssn", "dob"], "onMatch": "IGNORE"},
    },
    "events": EVENT,
}
LENIENT_ADDRESS = {
    "_meta": {
        "name": "LenientAddress",
        "immutable": True,
        "create": ["line1"],
        "upsert": {"where": ["line1"], "onMatchLinks": "IGNORE"},
    },
    "person": PERSON_REF,
}
CIRCLE: dict[str, Any] = {
    "_meta": {
        "name": "Circle",
        "immutable": True,
        "create": ["ssn", "dob", "name"],
        "upsert": {"where": ["ssn", "dob"]},
        "maxVisits": 2,
    }
}
CIRCLE["groups"] = {
    "_meta": {
        "name": "CircleGroup",
        "immutable": True,
        "create": ["name"],
        "upsert": {"where": ["name"]},
    },
    "members": CIRCLE,
}

# -- Mutable projections, matched with onMatch: UPDATE ------------------------

TAG = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["name"]},
        "unlink": True,
        "delete": True,
    }
}
AUTHOR = {
    "_meta": {
        "name": "EditAuthor",
        "create": ["name", "email"],
        "update": ["name"],
        "upsert": {"where": ["email"]},
        "onReplace": ["DISCONNECT", "DELETE"],
    }
}
POST = {
    "_meta": {
        "name": "EditPost",
        "create": ["title", "body"],
        "update": ["title"],
        "upsert": {"where": ["title"], "onMatch": "UPDATE"},
        "unlink": True,
        "delete": True,
    },
    "tags": TAG,
    "author": AUTHOR,
}
USER = {
    "_meta": {
        "name": "EditUser",
        "create": ["name", "email"],
        "update": ["name"],
        "upsert": {"where": ["email"], "onMatch": "UPDATE"},
    },
    "posts": POST,
}
PUBLISHER = {
    "_meta": {
        "name": "EditPublisher",
        "create": ["name"],
        "upsert": {"where": ["id"]},
    }
}
BOOK: dict[str, Any] = {
    "_meta": {
        "name": "EditBook",
        "create": ["title"],
        "update": ["title"],
        "upsert": {"where": ["id", "title"], "optional": True, "onMatch": "UPDATE"},
        "unlink": True,
        "delete": True,
    },
    "publisher": PUBLISHER,
}
BOOK["sequels"] = BOOK
BOOK["prequel"] = {
    "_meta": {"name": "EditPrequel", "create": ["title"]},
    "publisher": PUBLISHER,
}

ROOTS = {
    "person": ("Person", PERSON, "PersonInput"),
    "quiet_person": ("Person", QUIET_PERSON, "QuietPersonInput"),
    "address": ("Address", ADDRESS, "AddressInput"),
    "lenient_address": ("Address", LENIENT_ADDRESS, "LenientAddressInput"),
    "circle": ("Person", CIRCLE, "CircleInput"),
    "edit_user": ("User", USER, "EditUserInput"),
    "edit_post": ("Post", POST, "EditPostInput"),
    "edit_book": ("Book", BOOK, "EditBookInput"),
}

ADA = {"ssn": "111-11-1111", "dob": datetime.date(1990, 1, 1)}
ADA_WHERE = 'where: { ssn: "111-11-1111", dob: "1990-01-01" }'


async def resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _collect(*roots: type) -> dict[str, type]:
    """Every generated input class reachable from *roots*, by GraphQL name."""
    found: dict[str, type] = {}
    pending = list(roots)
    while pending:
        cls = pending.pop()
        definition = getattr(cls, "__strawberry_definition__", None)
        if not getattr(definition, "is_input", False) or definition.name in found:
            continue
        found[definition.name] = cls
        pending.extend(_declared_fields(cls).values())
    return found


def _mutation(orm: Any, input_type: type) -> Any:
    def resolver(info: strawberry.Info, input: Any) -> Any:
        row = orm.apply_upsert(input, info)
        if inspect.isawaitable(row):

            async def finish() -> int:
                return (await row).id

            return finish()
        return row.id

    resolver.__annotations__ = {
        "info": strawberry.Info,
        "input": input_type,
        "return": int,
    }
    return strawberry.mutation(resolver=resolver)


def build_schema(orm: Any, models: SimpleNamespace) -> SimpleNamespace:
    """The upsert inputs and a schema exposing each, built by *orm*."""

    @orm.type(models.Person)
    class PersonNode(relay.Node):
        id: relay.NodeID[int]
        name: auto

    @orm.type(models.Event)
    class EventNode(relay.Node):
        id: relay.NodeID[int]
        kind: auto

    inputs = {
        field_name: orm.mutations.upsert_node_input(
            model=getattr(models, model_name), project=project, name=name
        )
        for field_name, (model_name, project, name) in ROOTS.items()
    }
    mutation = strawberry.type(
        type(
            "Mutation",
            (),
            {name: _mutation(orm, input_type) for name, input_type in inputs.items()},
        )
    )
    query = strawberry.type(
        type("Query", (), {"__annotations__": {"ok": bool}, "ok": True})
    )
    schema = strawberry.Schema(
        query=query, mutation=mutation, types=[PersonNode, EventNode]
    )
    return SimpleNamespace(
        orm=orm, m=models, schema=schema, T=_collect(*inputs.values())
    )


# -- Harness -------------------------------------------------------------------

_TRANSACTION_CONTROL = {"BEGIN", "COMMIT", "SAVEPOINT", "RELEASE", "ROLLBACK"}


@dataclass
class UpsertDb:
    """One backend's database, as the tests below drive it."""

    built: SimpleNamespace
    #: Whether repo hooks may be ``async`` in this mode.
    async_hooks: bool
    statements: list[str] = field(default_factory=list)

    @property
    def m(self) -> SimpleNamespace:
        return self.built.m

    @property
    def T(self) -> dict[str, type]:
        return self.built.T

    @property
    def orm(self) -> Any:
        return self.built.orm

    @property
    def context(self) -> dict[str, Any]:
        return {}

    @property
    def info(self) -> Any:
        return SimpleNamespace(context=self.context)

    async def write(self, call: Callable[[], Any]) -> Any:
        """Run a library write the way this mode's resolvers would."""
        return await resolve(call())

    async def run_query(self, query: str) -> Any:
        return await self.built.schema.execute(query, context_value=self.context)

    async def execute(self, query: str) -> Any:
        result = await self.run_query(query)
        assert result.errors is None, result.errors
        return result.data

    async def error(self, query: str) -> BaseException:
        result = await self.run_query(query)
        assert result.errors, result.data
        return result.errors[0].original_error

    async def apply(self, payload: Any, **kwargs: Any) -> Any:
        return await self.write(
            lambda: self.orm.apply_upsert(payload, self.info, **kwargs)
        )

    async def apply_refs(self, instance: Any, name: str, refs: list[Any]) -> None:
        await self.write(
            lambda: self.orm.apply_ref_list(instance, name, refs, self.info)
        )

    def writes(self) -> list[str]:
        """The statements issued since the last :meth:`clear`, by verb."""
        verbs = [statement.split()[0].upper() for statement in self.statements]
        return [verb for verb in verbs if verb not in _TRANSACTION_CONTROL]

    def clear(self) -> None:
        self.statements.clear()

    # -- Per backend -----------------------------------------------------------

    async def create(self, model: type, **values: Any) -> Any:
        raise NotImplementedError

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        raise NotImplementedError

    async def rows(self, model: type) -> list[Any]:
        """Every row of *model*, by primary key."""
        raise NotImplementedError

    async def reload(self, instance: Any) -> Any:
        raise NotImplementedError

    async def related(self, instance: Any, name: str) -> list[Any]:
        raise NotImplementedError


class Fixtures:
    @pytest_asyncio.fixture
    async def ada(self, db):
        person = await db.create(db.m.Person, id=1, name="Ada", **ADA)
        db.clear()
        return person

    @pytest_asyncio.fixture
    async def blog(self, db):
        m = db.m
        alice = await db.create(m.User, id=1, name="Alice", email="alice@example.com")
        bob = await db.create(m.User, id=2, name="Bob", email="bob@example.com")
        python = await db.create(m.Tag, id=1, name="python")
        await db.create(m.Tag, id=2, name="rust")
        hello = await db.create(m.Post, id=1, title="Hello", body="B", author=alice)
        other = await db.create(m.Post, id=2, title="Other", body="B", author=bob)
        await db.add_related(hello, "tags", python)
        db.clear()
        return SimpleNamespace(alice=alice, bob=bob, hello=hello, other=other)

    @pytest_asyncio.fixture
    async def shelf(self, db):
        m = db.m
        acme = await db.create(m.Publisher, publisher_code="ACME", name="Acme")
        first = await db.create(m.Book, id=1, title="First", publisher=acme)
        await db.create(m.Book, id=2, title="Second", publisher=acme, prequel=first)
        await db.create(m.Book, id=3, title="Third", publisher=acme, prequel=first)
        db.clear()
        return SimpleNamespace(first=first)


# -- Find or create ------------------------------------------------------------


class AbstractTestFindOrCreate(Fixtures):
    async def test_a_miss_with_create_creates_the_row_seeded_from_where(self, db):
        await db.execute(
            f'mutation {{ person(input: {{ {ADA_WHERE} create: {{ name: "Ada" }} }}) }}'
        )
        [person] = await db.rows(db.m.Person)
        assert (person.ssn, person.dob, person.name) == (ADA["ssn"], ADA["dob"], "Ada")

    async def test_a_miss_without_create_is_not_found(self, db):
        error = await db.error(f"mutation {{ person(input: {{ {ADA_WHERE} }}) }}")
        assert isinstance(error, UpsertNotFoundError)
        assert await db.rows(db.m.Person) == []

    async def test_a_match_returns_the_row(self, db, ada):
        data = await db.execute(f"mutation {{ person(input: {{ {ADA_WHERE} }}) }}")
        assert data == {"person": 1}

    async def test_a_where_that_matches_several_rows_is_rejected(self, db, ada):
        for _ in range(2):
            await db.create(
                db.m.Event,
                kind="visit",
                occurred_on=datetime.date(2026, 1, 1),
                person=ada,
            )
        error = await db.error(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ events: [
                {{ upsert: {{ where: {{ kind: "visit", occurredOn: "2026-01-01" }} }} }}
            ] }} }}) }}"""
        )
        assert isinstance(error, UpsertValidationError)
        assert "more than one Event" in str(error)


# -- onMatch -------------------------------------------------------------------


class AbstractTestOnMatch(Fixtures):
    async def test_ignore_writes_nothing(self, db, ada):
        await db.execute(
            f"""mutation {{ quietPerson(input: {{ {ADA_WHERE} create: {{
                name: "Changed"
                events: [{{ create: {{ kind: "visit", occurredOn: "2026-01-01" }} }}]
            }} }}) }}"""
        )
        assert db.writes() == ["SELECT"]
        assert await db.rows(db.m.Event) == []

    async def test_append_inserts_children_and_leaves_the_parent_unchanged(
        self, db, ada
    ):
        await db.execute(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{
                name: "Changed"
                events: [
                    {{ create: {{ kind: "visit", occurredOn: "2026-01-01" }} }}
                    {{ create: {{ kind: "call", occurredOn: "2026-01-02" }} }}
                ]
            }} }}) }}"""
        )
        events = await db.rows(db.m.Event)
        assert [(e.kind, e.person_id) for e in events] == [("visit", 1), ("call", 1)]
        assert (await db.reload(ada)).name == "Ada"
        assert "UPDATE" not in db.writes()

    async def test_an_immutable_tree_issues_only_selects_and_inserts(self, db, ada):
        await db.create(db.m.Group, id=1, name="chess")
        db.clear()
        query = f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{
            events: [
                {{ create: {{ kind: "visit", occurredOn: "2026-01-01" }} }}
                {{ upsert: {{ where: {{ kind: "call", occurredOn: "2026-01-02" }}
                              create: {{}} }} }}
            ]
            groups: [
                {{ upsert: {{ where: {{ name: "chess" }} }} }}
                {{ upsert: {{ where: {{ name: "go" }}, create: {{}} }} }}
            ]
        }} }}) }}"""
        await db.execute(query)
        await db.execute(query.replace('ssn: "111-11-1111"', 'ssn: "222-22-2222"'))
        assert set(db.writes()) == {"SELECT", "INSERT"}
        assert len(await db.rows(db.m.Person)) == 2
        assert len(await db.rows(db.m.Group)) == 2

    async def test_nested_rows_under_a_matched_row_apply_their_own_on_match(
        self, db, ada
    ):
        day = datetime.date(2026, 1, 2)
        await db.create(db.m.Event, id=7, kind="call", occurred_on=day, person=ada)
        await db.execute(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ events: [
                {{ upsert: {{ where: {{ kind: "call", occurredOn: "2026-01-02" }}
                              create: {{}} }} }}
            ] }} }}) }}"""
        )
        assert [e.id for e in await db.rows(db.m.Event)] == [7]

    async def test_a_nested_lookup_is_scoped_to_its_parent(self, db, ada):
        grace = await db.create(db.m.Person, id=2, ssn="222-22-2222", dob=ADA["dob"])
        day = datetime.date(2026, 1, 2)
        await db.create(db.m.Event, kind="call", occurred_on=day, person=grace)
        await db.execute(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ events: [
                {{ upsert: {{ where: {{ kind: "call", occurredOn: "2026-01-02" }}
                              create: {{}} }} }}
            ] }} }}) }}"""
        )
        assert sorted(e.person_id for e in await db.rows(db.m.Event)) == [1, 2]

    async def test_a_matched_group_is_linked_to_the_matched_person(self, db, ada):
        await db.create(db.m.Group, id=1, name="chess")
        await db.execute(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ groups: [
                {{ upsert: {{ where: {{ name: "chess" }} }} }}
                {{ create: {{ name: "go" }} }}
            ] }} }}) }}"""
        )
        groups = await db.related(ada, "groups")
        assert sorted(group.name for group in groups) == ["chess", "go"]


# -- Forward links -------------------------------------------------------------


class AbstractTestForwardLinks(Fixtures):
    async def test_a_link_by_id_links_without_writing(self, db, ada):
        await db.execute(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            ' person: { upsert: { where: { id: "1" } } } } }) }'
        )
        [address] = await db.rows(db.m.Address)
        assert address.person_id == 1
        assert db.writes().count("INSERT") == 1
        assert "UPDATE" not in db.writes()

    async def test_a_link_by_id_that_matches_nothing_is_not_found(self, db, ada):
        error = await db.error(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            ' person: { upsert: { where: { id: "99" } } } } }) }'
        )
        assert isinstance(error, UpsertNotFoundError)
        assert await db.rows(db.m.Address) == []

    async def test_a_link_by_relay_global_id(self, db, ada):
        global_id = str(GlobalID("PersonNode", "1"))
        await db.execute(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            f' person: {{ upsert: {{ where: {{ id: "{global_id}" }} }} }} }} }}) }}'
        )
        [address] = await db.rows(db.m.Address)
        assert address.person_id == 1

    async def test_a_global_id_for_another_type_is_rejected(self, db, ada):
        global_id = str(GlobalID("EventNode", "1"))
        error = await db.error(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            f' person: {{ upsert: {{ where: {{ id: "{global_id}" }} }} }} }} }}) }}'
        )
        assert isinstance(error, UpsertValidationError)
        assert "global ID for EventNode" in str(error)

    async def test_an_id_that_is_not_a_key_is_rejected(self, db, ada):
        error = await db.error(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            ' person: { upsert: { where: { id: "abc" } } } } }) }'
        )
        assert "'abc' is not a valid Person id" in str(error)

    async def test_a_link_on_a_matched_row_is_rejected(self, db, ada):
        await db.create(db.m.Address, id=1, line1="1 Main", person=ada)
        error = await db.error(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            ' person: { upsert: { where: { id: "1" } } } } }) }'
        )
        assert isinstance(error, UpsertValidationError)
        assert "forward links are only written when the row is created" in str(error)

    async def test_on_match_links_ignore_skips_the_link(self, db, ada):
        await db.create(db.m.Address, id=1, line1="1 Main", person=ada)
        data = await db.execute(
            'mutation { lenientAddress(input: { where: { line1: "1 Main" } create: {'
            ' person: { upsert: { where: { id: "1" } } } } }) }'
        )
        assert data == {"lenientAddress": 1}

    async def test_a_link_can_create_its_target(self, db):
        await db.execute(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            ' person: { create: { ssn: "1", dob: "1990-01-01" } } } }) }'
        )
        [person] = await db.rows(db.m.Person)
        [address] = await db.rows(db.m.Address)
        assert address.person_id == person.id


# -- Validation before any write -----------------------------------------------


class AbstractTestValidation(Fixtures):
    async def test_an_empty_where_is_rejected(self, db):
        error = await db.error(
            'mutation { address(input: { where: { line1: "1 Main" } create: {'
            " person: { upsert: { where: {} } } } }) }"
        )
        assert isinstance(error, UpsertValidationError)
        assert "PersonRefWhere needs at least one field" in str(error)
        assert db.statements == []

    async def test_a_nested_item_cannot_re_point_its_parent(self, db):
        error = await db.error(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ addresses: [
                {{ create: {{ line1: "1 Main"
                              person: {{ upsert: {{ where: {{ id: "2" }} }} }} }} }}
            ] }} }}) }}"""
        )
        assert isinstance(error, UpsertValidationError)
        assert "AddressCreate.person cannot be set" in str(error)
        assert db.statements == []

    async def test_where_and_create_must_agree(self, db):
        error = await db.error(
            f'mutation {{ person(input: {{ {ADA_WHERE} create: {{ ssn: "999" }} }}) }}'
        )
        assert "where.ssn and create.ssn differ" in str(error)

    async def test_the_depth_limit_is_enforced(self, db):
        error = await db.error(
            """mutation { circle(input: {
              where: { ssn: "S", dob: "1990-01-01" }
              create: { groups: [{ upsert: { where: { name: "G" }, create: { members: [
                { upsert: { where: { ssn: "T", dob: "1990-01-01" }, create: { groups: [
                  { upsert: { where: { name: "H" }, create: { members: [
                    { upsert: { where: { ssn: "U", dob: "1990-01-01" } } }
                  ] } } }
                ] } } }
              ] } } }] }
            }) }"""
        )
        assert isinstance(error, UpsertValidationError)
        assert "Person appears 3 times" in str(error)
        assert db.statements == []

    async def test_the_depth_limit_allows_its_own_depth(self, db):
        await db.execute(
            'mutation { circle(input: { where: { ssn: "S", dob: "1990-01-01" }'
            ' create: { groups: [{ upsert: { where: { name: "G" }, create: { members: ['
            '{ upsert: { where: { ssn: "T", dob: "1990-01-01" }, create: {} } }'
            "] } } }] } }) }"
        )
        assert len(await db.rows(db.m.Person)) == 2

    async def test_an_error_part_way_leaves_no_rows(self, db):
        error = await db.error(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ events: [
                {{ create: {{ kind: "visit", occurredOn: "2026-01-01" }} }}
                {{ upsert: {{ where: {{ kind: "call", occurredOn: "2026-01-02" }} }} }}
            ] }} }}) }}"""
        )
        assert isinstance(error, UpsertNotFoundError)
        assert "INSERT" in db.writes()
        assert await db.rows(db.m.Person) == []
        assert await db.rows(db.m.Event) == []


def person_payload(db: UpsertDb, **create: Any) -> Any:
    T = db.T
    return T["PersonInput"](
        where=T["PersonWhere"](**ADA), create=T["PersonCreate"](**create)
    )


def address_linked_to(db: UpsertDb, pk: Any) -> Any:
    T = db.T
    link = T["AddressPersonLink"](
        upsert=T["PersonRefUpsert"](where=T["PersonRefWhere"](id=pk))
    )
    return T["AddressInput"](
        where=T["AddressWhere"](line1="1 Main"),
        create=T["AddressCreate"](line1="1 Main", person=link),
    )


class AbstractTestHandBuiltPayloads(Fixtures):
    """Payloads built in Python skip schema validation; the runtime does not."""

    async def test_a_disallowed_operation_is_rejected(self, db):
        T = db.T
        item = T["PersonEventsItem"](create=T["EventCreate"](kind="visit"))
        item.create = strawberry.UNSET
        item.update = T["EventCreate"](kind="visit")
        with pytest.raises(UpsertValidationError, match="does not allow the update"):
            await db.apply(person_payload(db, events=[item]))
        assert db.statements == []

    async def test_two_operations_at_once_are_rejected(self, db):
        T = db.T
        where = T["EventWhere"](kind="visit", occurred_on=datetime.date(2026, 1, 1))
        item = T["PersonEventsItem"](
            create=T["EventCreate"](kind="visit"), upsert=T["EventUpsert"](where=where)
        )
        with pytest.raises(
            UpsertValidationError, match="exactly one of create, upsert"
        ):
            await db.apply(person_payload(db, events=[item]))

    async def test_an_update_on_an_insert_only_upsert_is_rejected(self, db):
        payload = person_payload(db)
        payload.update = db.T["PersonCreate"]()
        with pytest.raises(UpsertValidationError, match="takes no update"):
            await db.apply(payload)

    async def test_a_payload_of_the_wrong_type_is_rejected(self, db):
        T = db.T
        payload = T["PersonInput"](
            where=T["PersonWhere"](**ADA), create=T["EventCreate"](kind="x")
        )
        with pytest.raises(UpsertValidationError, match="Expected PersonCreate"):
            await db.apply(payload)

    async def test_a_list_relation_must_be_a_list(self, db):
        with pytest.raises(UpsertValidationError, match="must be a list"):
            await db.apply(person_payload(db, events="visit"))

    async def test_where_is_required(self, db):
        with pytest.raises(UpsertValidationError, match="requires where"):
            await db.apply(db.T["PersonInput"](where=strawberry.UNSET))

    async def test_a_null_id_is_rejected(self, db):
        with pytest.raises(UpsertValidationError, match="id cannot be null"):
            await db.apply(address_linked_to(db, None))

    async def test_a_raw_integer_id_is_used_as_is(self, db, ada):
        address = await db.apply(address_linked_to(db, 1))
        assert address.person_id == 1

    async def test_an_input_not_built_by_upsert_node_input_is_rejected(self, db):
        with pytest.raises(TypeError, match="upsert_node_input"):
            await db.apply(db.T["PersonCreate"]())

    async def test_an_unknown_on_replace_is_rejected(self, db, blog):
        T = db.T
        link = T["EditPostAuthorLink"](
            update=T["EditAuthorUpdate"](id="1"), on_replace="BOGUS"
        )
        payload = T["EditPostInput"](
            where=T["EditPostWhere"](title="Hello"),
            update=T["EditPostPatch"](author=link),
        )
        with pytest.raises(UpsertValidationError, match="does not allow onReplace"):
            await db.apply(payload)


# -- Authorization and repo hooks ----------------------------------------------


class CountingRepo(AbstractRepo):
    found: list[dict[str, Any]] = []
    matched: list[Any] = []

    def find_for_upsert(self, model, query, info):
        type(self).found.append(dict(query))
        return super().find_for_upsert(model, query, info)

    def on_match(self, instance, info):
        type(self).matched.append(instance.id)


class DenyingRepo(AbstractRepo):
    def can_upsert(self, instance, info):
        return False


class AsyncHooksRepo(AbstractRepo):
    async def find_for_upsert(self, model, query, info):
        return None

    async def on_match(self, instance, info):
        return None


class AbstractTestHooks(Fixtures):
    @pytest.fixture(autouse=True)
    def _reset(self):
        CountingRepo.found, CountingRepo.matched = [], []

    async def test_find_and_on_match_run_once_per_row(self, db, ada, monkeypatch):
        repos = db.orm._backend._repos
        monkeypatch.setitem(repos, db.m.Person, CountingRepo)
        monkeypatch.setitem(repos, db.m.Event, CountingRepo)
        await db.create(
            db.m.Event,
            id=5,
            kind="call",
            occurred_on=datetime.date(2026, 1, 2),
            person=ada,
        )
        await db.execute(
            f"""mutation {{ person(input: {{ {ADA_WHERE} create: {{ events: [
                {{ upsert: {{ where: {{ kind: "call", occurredOn: "2026-01-02" }} }} }}
                {{ upsert: {{ where: {{ kind: "visit", occurredOn: "2026-01-03" }}
                              create: {{}} }} }}
            ] }} }}) }}"""
        )
        assert CountingRepo.matched == [1, 5]
        assert CountingRepo.found == [
            ADA,
            {"kind": "call", "occurred_on": datetime.date(2026, 1, 2), "person_id": 1},
            {"kind": "visit", "occurred_on": datetime.date(2026, 1, 3), "person_id": 1},
        ]

    async def test_an_async_finder_needs_async_hooks(self, db, ada, monkeypatch):
        monkeypatch.setitem(db.orm._backend._repos, db.m.Person, AsyncHooksRepo)
        if not db.async_hooks:
            with pytest.raises(TypeError, match="returned an awaitable"):
                await db.apply(person_payload(db))
            return
        await db.apply(person_payload(db))
        assert len(await db.rows(db.m.Person)) == 2

    async def test_an_async_on_match_needs_async_hooks(self, db, ada, monkeypatch):
        class MatchOnly(AsyncHooksRepo):
            find_for_upsert = AbstractRepo.find_for_upsert

        monkeypatch.setitem(db.orm._backend._repos, db.m.Person, MatchOnly)
        if db.async_hooks:
            assert (await db.apply(person_payload(db))).id == 1
            return
        with pytest.raises(TypeError, match="returned an awaitable"):
            await db.apply(person_payload(db))

    async def test_a_denied_match_raises(self, db, ada, monkeypatch):
        monkeypatch.setitem(db.orm._backend._repos, db.m.Person, DenyingRepo)
        with pytest.raises(PermissionError, match="can_upsert"):
            await db.apply(person_payload(db))

    async def test_authorize_sees_upsert_then_create(self, db, ada):
        seen: list[tuple[str, str, Any]] = []

        def authorize(action, model, pk, info):
            seen.append((action, model.__name__, pk))
            return action != "create"

        T = db.T
        payload = person_payload(
            db,
            events=[
                T["PersonEventsItem"](
                    create=T["EventCreate"](kind="x", occurred_on=ADA["dob"])
                )
            ],
        )
        with pytest.raises(PermissionError, match="denied the create operation"):
            await db.apply(payload, authorize=authorize)
        assert seen == [("upsert", "Person", 1), ("create", "Event", None)]
        assert await db.rows(db.m.Event) == []


# -- apply_ref_list -------------------------------------------------------------


class AbstractTestApplyRefList(Fixtures):
    async def test_upsert_items_are_applied_to_the_parent(self, db, ada):
        T = db.T
        await db.create(
            db.m.Event,
            id=5,
            kind="call",
            occurred_on=datetime.date(2026, 1, 2),
            person=ada,
        )
        item, where = T["PersonEventsItem"], T["EventWhere"]
        await db.apply_refs(
            ada,
            "events",
            [
                item(
                    upsert=T["EventUpsert"](
                        where=where(kind="call", occurred_on=datetime.date(2026, 1, 2))
                    )
                ),
                item(
                    upsert=T["EventUpsert"](
                        where=where(kind="new", occurred_on=datetime.date(2026, 1, 3)),
                        create=T["EventCreate"](),
                    )
                ),
            ],
        )
        events = await db.rows(db.m.Event)
        assert [(e.id, e.kind, e.person_id) for e in events] == [
            (5, "call", 1),
            (6, "new", 1),
        ]

    async def test_a_matched_group_is_linked(self, db, ada):
        T = db.T
        await db.create(db.m.Group, id=1, name="chess")
        upsert = T["GroupUpsert"](where=T["GroupWhere"](name="chess"))
        await db.apply_refs(ada, "groups", [T["PersonGroupsItem"](upsert=upsert)])
        assert [group.name for group in await db.related(ada, "groups")] == ["chess"]

    async def test_a_disallowed_item_is_rejected_before_any_write(self, db, ada):
        T = db.T
        item = T["PersonEventsItem"](create=T["EventCreate"](kind="x"))
        item.delete = SimpleNamespace(id="1")
        with pytest.raises(UpsertValidationError, match="exactly one"):
            await db.apply_refs(ada, "events", [item])
        assert db.statements == []

    async def test_a_hand_made_ref_type_may_carry_a_generated_upsert(self, db, ada):
        T = db.T
        ref = make_ref_type(
            db.m.Event, upsert=T["EventUpsert"], update=False, name="EventRefX"
        )
        await db.apply_refs(
            ada,
            "events",
            [
                ref(
                    upsert=T["EventUpsert"](
                        where=T["EventWhere"](kind="x", occurred_on=ADA["dob"]),
                        create=T["EventCreate"](),
                    )
                )
            ],
        )
        assert [e.kind for e in await db.rows(db.m.Event)] == ["x"]

    async def test_a_hand_written_upsert_type_is_rejected(self, db, ada):
        @strawberry.input
        class LooseUpsert:
            kind: str

        ref = make_ref_type(
            db.m.Event, upsert=LooseUpsert, update=False, name="EventRefY"
        )
        with pytest.raises(TypeError, match="must be built by orm.mutations"):
            await db.apply_refs(ada, "events", [ref(upsert=LooseUpsert(kind="x"))])

    async def test_an_error_part_way_leaves_no_rows(self, db, ada):
        T = db.T
        item, where = T["PersonEventsItem"], T["EventWhere"]
        refs = [
            item(
                upsert=T["EventUpsert"](
                    where=where(kind="new", occurred_on=ADA["dob"]),
                    create=T["EventCreate"](),
                )
            ),
            item(
                upsert=T["EventUpsert"](
                    where=where(kind="gone", occurred_on=ADA["dob"])
                )
            ),
        ]
        with pytest.raises(UpsertNotFoundError):
            await db.apply_refs(ada, "events", refs)
        assert await db.rows(db.m.Event) == []


# -- onMatch: UPDATE, with every nested operation -------------------------------


class AbstractTestUpdateOnMatch(Fixtures):
    async def test_a_match_applies_update_and_its_nested_items(self, db, blog):
        await db.execute(
            """mutation { editUser(input: {
                where: { email: "alice@example.com" }
                update: { name: "Alicia", posts: [
                    { update: { id: "1", title: "Hello again" } }
                    { update: { id: "2" } }
                    { create: { title: "New", body: "B" } }
                    { upsert: { where: { title: "Fresh" }, create: { body: "B" } } }
                ] }
            }) }"""
        )
        posts = await db.rows(db.m.Post)
        assert [(p.title, p.author_id) for p in posts] == [
            ("Hello again", 1),
            ("Other", 1),
            ("New", 1),
            ("Fresh", 1),
        ]
        assert (await db.reload(blog.alice)).name == "Alicia"

    async def test_a_match_without_update_changes_nothing(self, db, blog):
        await db.execute(
            'mutation { editUser(input: { where: { email: "alice@example.com" } }) }'
        )
        assert "UPDATE" not in db.writes()

    async def test_list_items_link_unlink_and_delete(self, db, blog):
        await db.execute(
            """mutation { editPost(input: {
                where: { title: "Hello" }
                update: { tags: [
                    { unlink: { id: "1" } }
                    { update: { id: "2", name: "rusty" } }
                    { upsert: { where: { name: "go" }, create: {} } }
                    { upsert: { where: { name: "rusty" } } }
                ] }
            }) }"""
        )
        tags = await db.related(blog.hello, "tags")
        assert sorted(tag.name for tag in tags) == ["go", "rusty"]
        await db.execute(
            """mutation { editPost(input: {
                where: { title: "Hello" }
                update: { tags: [{ delete: { id: "2" } }] }
            }) }"""
        )
        assert sorted(tag.name for tag in await db.rows(db.m.Tag)) == ["go", "python"]

    async def test_removing_a_row_that_is_not_a_member_is_not_found(self, db, blog):
        for tag_id in ("2", "99"):
            error = await db.error(
                f"""mutation {{ editPost(input: {{
                    where: {{ title: "Hello" }}
                    update: {{ tags: [{{ unlink: {{ id: "{tag_id}" }} }}] }}
                }}) }}"""
            )
            assert isinstance(error, UpsertNotFoundError)

    async def test_an_update_by_an_unknown_id_is_not_found(self, db, blog):
        error = await db.error(
            """mutation { editPost(input: {
                where: { title: "Hello" }
                update: { tags: [{ update: { id: "99" } }] }
            }) }"""
        )
        assert isinstance(error, UpsertNotFoundError)

    async def test_a_required_foreign_key_cannot_be_unlinked(self, db, blog):
        error = await db.error(
            """mutation { editUser(input: {
                where: { email: "alice@example.com" }
                update: { posts: [{ unlink: { id: "1" } }] }
            }) }"""
        )
        assert isinstance(error, UpsertValidationError)
        assert "its foreign key is not nullable" in str(error)
        assert db.statements == []

    async def test_re_pointing_a_link_can_delete_the_old_target(self, db, blog):
        await db.execute(
            """mutation { editPost(input: {
                where: { title: "Other" }
                update: { author: {
                    upsert: { where: { email: "carol@example.com" }, create: { name: "Carol" } }
                    onReplace: DELETE
                } }
            }) }"""
        )
        assert [user.name for user in await db.rows(db.m.User)] == ["Alice", "Carol"]
        assert [post.title for post in await db.rows(db.m.Post)] == ["Hello", "Other"]

    async def test_re_pointing_disconnects_by_default(self, db, blog):
        await db.execute(
            """mutation { editPost(input: {
                where: { title: "Other" }
                update: { author: { update: { id: "1", name: "Al" } } }
            }) }"""
        )
        users = await db.rows(db.m.User)
        assert [user.name for user in users] == ["Al", "Bob"]
        assert (await db.reload(blog.other)).author_id == 1

    async def test_re_pointing_to_the_same_row_keeps_it(self, db, blog):
        await db.execute(
            """mutation { editPost(input: {
                where: { title: "Other" }
                update: { author: {
                    upsert: { where: { email: "bob@example.com" } }, onReplace: DELETE
                } }
            }) }"""
        )
        assert len(await db.rows(db.m.User)) == 2


class AbstractTestBooks(Fixtures):
    """A recursive projection, a string primary key, and a nullable back-reference."""

    async def test_sequels_can_be_unlinked_and_deleted(self, db, shelf):
        await db.execute(
            """mutation { editBook(input: {
                where: { id: "1" }
                update: { sequels: [{ unlink: { id: "2" } }, { delete: { id: "3" } }] }
            }) }"""
        )
        books = await db.rows(db.m.Book)
        assert [(b.id, b.prequel_id) for b in books] == [(1, None), (2, None)]

    async def test_a_link_can_be_set_where_there_was_none(self, db, shelf):
        await db.execute(
            """mutation { editBook(input: {
                where: { title: "First" }
                update: { prequel: { create: {
                    title: "Zeroth", publisher: { upsert: { where: { id: "ACME" } } }
                } } }
            }) }"""
        )
        assert (await db.reload(shelf.first)).prequel_id == 4

    async def test_a_missing_string_key_is_not_found(self, db, shelf):
        error = await db.error(
            """mutation { editBook(input: {
                where: { title: "Fourth" }
                create: { publisher: { upsert: { where: { id: "NOPE" } } } }
            }) }"""
        )
        assert isinstance(error, UpsertNotFoundError)
