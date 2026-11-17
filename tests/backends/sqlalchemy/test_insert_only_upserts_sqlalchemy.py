"""Upsert cases only SQLAlchemy can express.

A relationship without a back-reference ties a nested row to its parent by the
foreign key column alone, and a column type may have no Python type.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Integer

from strawberry_orm.upserts import _declared_fields
from tests.abstract.insert_only_upserts import ADA, _collect, address_linked_to
from tests.backends.sqlalchemy.insert_only_harness import ORM
from tests.backends.sqlalchemy.insert_only_models import Note, Person

NOTED_PERSON = {
    "_meta": {
        "name": "NotedPerson",
        "immutable": True,
        "create": ["ssn", "dob"],
        "upsert": {"where": ["ssn", "dob"]},
    },
    "notes": {"_meta": {"name": "Note", "immutable": True, "create": ["body"]}},
}
N = _collect(
    ORM.mutations.upsert_node_input(
        model=Person, project=NOTED_PERSON, name="NotedPersonInput"
    )
)
NOTED_PERSON_WITH_KEY = {
    **NOTED_PERSON,
    "notes": {
        "_meta": {
            "name": "KeyedNote",
            "immutable": True,
            "create": ["body", "person_id"],
        }
    },
}


def _noted_person(**note: Any) -> Any:
    item = N["NotedPersonNotesItem"](create=N["NoteCreate"](**note))
    return N["NotedPersonInput"](
        where=N["NotedPersonWhere"](**ADA), create=N["NotedPersonCreate"](notes=[item])
    )


class TestColumnOnlyRelations:
    async def test_a_note_without_a_back_reference_is_tied_by_its_column(self, db):
        await db.apply(_noted_person(body="hi"))
        [note] = await db.rows(Note)
        [person] = await db.rows(Person)
        assert note.person_id == person.id

    def test_a_key_column_without_a_relationship_is_not_offered(self):
        assert list(_declared_fields(N["NoteCreate"])) == ["body"]
        with pytest.raises(
            ValueError,
            match=r"Unknown field\(s\) in _meta.create for Note: person_id",
        ):
            ORM.mutations.upsert_node_input(
                model=Person, project=NOTED_PERSON_WITH_KEY, name="KeyedPersonInput"
            )

    def test_a_hand_written_input_offers_the_key_column_only_when_named(self):
        assert list(_declared_fields(ORM.input(Note, name="NoteFields"))) == ["body"]
        named = ORM.input(Note, include=["body", "person_id"], name="NoteKeyFields")
        assert list(_declared_fields(named)) == ["body", "person_id"]


class TestColumnTypes:
    async def test_an_id_column_without_a_python_type_is_passed_through(
        self, db, monkeypatch
    ):
        await db.create(Person, id=1, name="Ada", **ADA)

        def no_python_type(self: Any) -> type:
            raise NotImplementedError

        monkeypatch.setattr(Integer, "python_type", property(no_python_type))
        address = await db.apply(address_linked_to(db, "1"))
        assert address.person_id == 1
