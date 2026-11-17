"""A write's errors name a row's model as a client knows it, or not at all.

The model's class is the app's own: its GraphQL type can be named otherwise,
and a model with no type, or more than one, has no one name a client knows.
An error names the model's one type, ``Document`` for ``WaFile`` here, and
otherwise none.

Each backend supplies the ``writes`` fixture of :mod:`write_path_stored_key`.
"""

from typing import Any

import pytest
import pytest_asyncio

from strawberry_orm import UpsertNotFoundError, UpsertValidationError
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import item, member

#: Per kind of schema: the name a file is known by, and a shelf's.
NAMES = {
    "typed": ("Document", "Bookcase"),
    "untyped": (None, None),
    "two file types": (None, "Bookcase"),
}


def deny(action, model, pk, info):
    return False


class AbstractTestErrorsNameTheClientsType:
    """Shelf 1 holds file ``plain``, and files ``a`` and ``b`` titled ``twin``."""

    @pytest_asyncio.fixture
    async def shelf(self, writes):
        m = writes.m
        shelf = await writes.create(m.Shelf, id=1, label="shelf")
        for key, title in (("plain", "plain"), ("a", "twin"), ("b", "twin")):
            await writes.create(m.File, id=key, title=title, shelf=shelf)
        return shelf

    @pytest.fixture(params=list(NAMES))
    def schema(self, request, writes):
        """The orm, with the types *request* asks for, and the names they give."""
        orm = writes.new_orm()
        m = writes.m
        if request.param != "untyped":

            @orm.type(m.File)
            class Document:
                id: auto
                title: auto

            @orm.type(m.Shelf)
            class Bookcase:
                label: auto

        if request.param == "two file types":

            @orm.type(m.File)
            class Draft:
                title: auto

        return orm, NAMES[request.param]

    @staticmethod
    def by_title(orm: Any, writes: Any) -> type:
        return orm.mutations.upsert_node_input(
            model=writes.m.File,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": ["title"], "onMatch": "UPDATE"},
                }
            },
            name="WaFileByTitle",
        )

    @staticmethod
    def where_title(upsert: type, title: str) -> Any:
        return upsert(where=member(upsert, "where")(title=title))

    async def test_a_missing_member_is_named_as_the_client_knows_it(
        self, writes, shelf, schema
    ):
        orm, (file, shelf_type) = schema
        ref = orm.ref(writes.m.File, delete=True)
        with pytest.raises(UpsertNotFoundError) as raised:
            await writes.refs(orm, shelf, "files", [item(ref, "delete", id="gone")])
        relation = f"{shelf_type}.files" if shelf_type else "files"
        assert str(raised.value) == f"No {file or 'row'} 'gone' in {relation}"

    async def test_an_upsert_miss_is_named_as_the_client_knows_it(
        self, writes, shelf, schema
    ):
        orm, (file, _shelf) = schema
        payload = self.where_title(self.by_title(orm, writes), "nope")
        with pytest.raises(UpsertNotFoundError) as raised:
            await writes.upsert(orm, payload)
        assert str(raised.value) == (
            f"No {file or 'row'} matches {{'title': 'nope'}} and no create was sent"
        )

    async def test_an_ambiguous_match_is_named_as_the_client_knows_it(
        self, writes, shelf, schema
    ):
        orm, (file, _shelf) = schema
        payload = self.where_title(self.by_title(orm, writes), "twin")
        with pytest.raises(UpsertValidationError) as raised:
            await writes.upsert(orm, payload)
        assert str(raised.value) == (
            f"The upsert where matches more than one {file or 'row'}; match on "
            f"fields that identify a single row"
        )

    async def test_an_authorize_denial_is_named_as_the_client_knows_it(
        self, writes, shelf, schema
    ):
        orm, (file, _shelf) = schema
        payload = self.where_title(self.by_title(orm, writes), "plain")
        with pytest.raises(PermissionError) as raised:
            await writes.upsert(orm, payload, authorize=deny)
        on = f" on {file}" if file else ""
        assert str(raised.value) == f"authorize denied the upsert operation{on}"

    async def test_an_id_that_is_not_one_names_no_model(self, writes, shelf):
        orm = writes.new_orm()
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Shelf,
            project={"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
            name="WaShelfById",
        )
        with pytest.raises(UpsertValidationError) as raised:
            await writes.upsert(orm, upsert(where=member(upsert, "where")(id="abc")))
        assert str(raised.value) == "'abc' is not a valid id"
