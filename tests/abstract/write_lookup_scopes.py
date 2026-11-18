"""A write's lookup counts only the rows every repo governing them shows.

A row of a subclass is a row of each model it inherits, and a row a subclass
holds is that subclass's (see ``write_path_subclass_rows``), so the writer
reads a row as absent unless the repo of each of those models shows it. The
upsert lookup counts and picks among the rows its ``where`` matches before the
writer reads them: counted, a row one of those repos hides makes a match
ambiguous, and so tells the caller that a row it cannot see holds the value.

Each way a repo can govern a doc the caller cannot see, in :data:`SCENARIOS`,
is crossed with each write that looks a row up by ``where``: the root of
``upsert_node_input()``; the ``upsert`` of a many-to-many list item, nested in
an upsert and sent to ``orm.apply_ref_list()``; that of a single link; and,
through the base, that of a reverse foreign key list item, whose lookup is
narrowed to the parent's rows. Each cell looks the hidden doc up by a title a
visible doc shares, which has to find the visible doc, and by a title and by a
key the hidden doc alone holds, which have to find nothing.

A row one subclass's table holds can be held by a sibling's as well, a sealed
doc that is a public doc too, and is a row of that sibling then. Each way the
sibling's repo can govern it, in :data:`SIBLINGS`, is crossed with the same
writes and lookups.

Each backend binds :class:`AbstractTestWriteLookupScopes` with a ``writes``
fixture of the shape ``test_write_path_subclass_rows`` supplies, and
:class:`AbstractTestSiblingLookupScopes` where its ``also_a`` can make a row a
sibling's too.
"""

from dataclasses import dataclass
from typing import Any

import pytest

from strawberry_orm import AbstractRepo, UpsertNotFoundError
from tests.abstract.write_path_authorization import member
from tests.abstract.write_path_subclass_rows import recorder

DOCS = ("Doc", "SealedDoc", "RestrictedDoc", "PublicDoc")
VISIBLE, HIDDEN = 1, 2
TITLE = "twin"


@dataclass(frozen=True)
class Scenario:
    #: The model the write looks the doc up through.
    entry: str
    #: The model of the doc the caller cannot see.
    hidden: str
    #: The model whose repo hides it.
    hider: str
    #: Whether one repo class answers for every doc model, hiding it as *hider*'s.
    shared: bool = False
    #: Whether *entry* has a repo of its own, rather than its nearest base's.
    registered: bool = True
    #: The sibling of *hidden* whose table holds the doc too, if any.
    also: str | None = None


SCENARIOS = {
    "a base hides it": Scenario("SealedDoc", "SealedDoc", "Doc"),
    "a base of a base hides it": Scenario("RestrictedDoc", "RestrictedDoc", "Doc"),
    "the nearest base hides it": Scenario(
        "RestrictedDoc", "RestrictedDoc", "SealedDoc"
    ),
    "a base of a base hides it from a model with its base's repo": Scenario(
        "RestrictedDoc", "RestrictedDoc", "Doc", registered=False
    ),
    "a base hides it through a shared repo class": Scenario(
        "SealedDoc", "SealedDoc", "Doc", shared=True
    ),
    "its own repo hides it": Scenario("SealedDoc", "SealedDoc", "SealedDoc"),
    "a subclass hides it": Scenario("Doc", "SealedDoc", "SealedDoc"),
    "a subclass hides it through a shared repo class": Scenario(
        "Doc", "SealedDoc", "SealedDoc", shared=True
    ),
    "a subclass of a subclass hides it": Scenario(
        "Doc", "RestrictedDoc", "RestrictedDoc"
    ),
    "a subclass hides it that its own subclass shows": Scenario(
        "Doc", "RestrictedDoc", "SealedDoc"
    ),
    "a subclass of the entry hides it": Scenario(
        "SealedDoc", "RestrictedDoc", "RestrictedDoc"
    ),
}

SIBLINGS = {
    "a sibling hides it": Scenario(
        "SealedDoc", "SealedDoc", "PublicDoc", also="PublicDoc"
    ),
    "a sibling of a base hides it": Scenario(
        "RestrictedDoc", "RestrictedDoc", "PublicDoc", also="PublicDoc"
    ),
    "a sibling hides it from a model with its base's repo": Scenario(
        "SealedDoc", "SealedDoc", "PublicDoc", registered=False, also="PublicDoc"
    ),
    "a sibling hides it through a shared repo class": Scenario(
        "SealedDoc", "SealedDoc", "PublicDoc", shared=True, also="PublicDoc"
    ),
    "one of two subclasses holding it hides it": Scenario(
        "Doc", "SealedDoc", "PublicDoc", also="PublicDoc"
    ),
}

ENTRIES = ("root", "list", "ref list", "link", "reverse foreign key list")
#: The relations of ``Binder`` and of ``Mark`` holding each model's docs.
LISTS = {"Doc": "docs", "SealedDoc": "sealed", "RestrictedDoc": "restricted"}
LINKS = {"Doc": "doc", "SealedDoc": "sealed", "RestrictedDoc": "restricted"}
#: Each lookup, and whether it finds the visible doc rather than nothing.
LOOKUPS = {"shared title": True, "own title": False, "key": False}


def cells(scenarios: dict[str, Scenario]) -> list[Any]:
    return [
        pytest.param(scenario, entry, lookup, id=f"{scenario}-{entry}-{lookup}")
        for scenario, case in scenarios.items()
        for entry in ENTRIES
        if entry != "reverse foreign key list" or case.entry == "Doc"
        for lookup in LOOKUPS
    ]


def repos(writes: Any, calls: list, case: Scenario) -> dict[type, type]:
    """A repo for each doc model, each showing every doc but *case*'s hider's."""
    m = writes.m
    names = [name for name in DOCS if case.registered or name != case.entry]
    if not case.shared:
        return {
            getattr(m, name): recorder(
                writes, calls, name, [VISIBLE] if name == case.hider else None
            )
            for name in names
        }
    hider = getattr(m, case.hider)

    class Shared(AbstractRepo):
        def scope_query(self, query, info):
            if self.model is hider:
                return writes.only(query, hider, [VISIBLE])
            return query

        def can_update(self, instance, data, info):
            calls.append(("shared", "can_update", instance.id))
            return True

    return {getattr(m, name): Shared for name in names}


def rename(upsert: type, where: dict[str, str]) -> Any:
    return upsert(
        where=member(upsert, "where")(**where),
        update=member(upsert, "update")(title="edited"),
    )


def where_of(lookup: str) -> dict[str, str]:
    return {"id": str(HIDDEN)} if lookup == "key" else {"title": TITLE}


def by(orm: Any, model: type, where: dict[str, str]) -> type:
    return orm.mutations.upsert_node_input(
        model=model,
        project={
            "_meta": {
                "update": ["title"],
                "upsert": {"where": list(where), "onMatch": "UPDATE"},
            }
        },
    )


async def check_missing(
    writes: Any, write: Any, lookup: str, calls: list, read: type
) -> None:
    """Run *write*, and check it read the hidden row as missing.

    The *read* rows are the visible row, which the ``shared title`` lookup
    renames, and the hidden one, which no lookup may touch or ask a repo about.
    """
    finds = LOOKUPS[lookup]
    if finds:
        await write
    else:
        with pytest.raises(UpsertNotFoundError):
            await write
    titles = {row.id: row.title for row in await writes.rows(read)}
    expected = {HIDDEN: TITLE, **({VISIBLE: "edited"} if finds else {})}
    assert titles == expected, f"the write left {titles}, not {expected}"
    asked = [call for call in calls if call[2] == HIDDEN]
    assert not asked, f"the repos were asked about the hidden row: {asked}"


async def look_up(writes: Any, case: Scenario, entry: str, lookup: str) -> None:
    """Look *case*'s hidden doc up through *entry* by *lookup*, and check the
    write read it as missing and linked only the visible doc, if anything.

    Doc 1, of the entry model, is visible, and only the ``shared title``
    lookup has it; doc 2, of the scenario's hidden model, is hidden by the
    scenario's hider. Both are in folder 1 and hold the same title.
    """
    m = writes.m
    folder = await writes.create(m.Folder, id=1)
    if LOOKUPS[lookup]:
        model = getattr(m, case.entry)
        await writes.create(model, id=VISIBLE, title=TITLE, folder=folder)
    hidden = getattr(m, case.hidden)
    await writes.create(hidden, id=HIDDEN, title=TITLE, folder=folder)
    if case.also is not None:
        await writes.also_a(getattr(m, case.also), HIDDEN)
    binder = await writes.create(m.Binder, id=1)
    await writes.create(m.Mark, id=1, label="mark")
    calls: list = []
    orm = writes.new_orm(repos=repos(writes, calls, case))
    write = write_through(orm, writes, binder, case.entry, entry, where_of(lookup))
    await check_missing(writes, write, lookup, calls, m.Doc)
    held = await held_by(writes, binder, case.entry)
    linked = LOOKUPS[lookup] and entry in ("list", "ref list", "link")
    expected = [VISIBLE] if linked else []
    assert held == expected, f"binder 1 and mark 1 hold {held}, not {expected}"


def write_through(
    orm: Any, writes: Any, binder: Any, model: str, entry: str, where: Any
) -> Any:
    """The *entry* write looking up a *model* doc by *where* to rename it."""
    m = writes.m
    if entry == "root":
        return writes.upsert(orm, rename(by(orm, getattr(m, model), where), where))
    holder, relation, key, value = {
        "list": (m.Binder, LISTS[model], "id", "1"),
        "ref list": (m.Binder, LISTS[model], "id", "1"),
        "link": (m.Mark, LINKS[model], "label", "mark"),
        "reverse foreign key list": (m.Folder, "docs", "id", "1"),
    }[entry]
    outer = orm.mutations.upsert_node_input(
        model=holder,
        project={
            "_meta": {
                "update": [],
                "upsert": {"where": [key], "onMatch": "UPDATE"},
            },
            relation: {
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": list(where), "onMatch": "UPDATE"},
                }
            },
        },
    )
    container = member(outer, "update", relation)
    item = container(upsert=rename(member(container, "upsert"), where))
    if entry == "ref list":
        return writes.refs(orm, binder, relation, [item])
    sent = item if entry == "link" else [item]
    return writes.upsert(
        orm,
        outer(
            where=member(outer, "where")(**{key: value}),
            update=member(outer, "update")(**{relation: sent}),
        ),
    )


async def held_by(writes: Any, binder: Any, model: str) -> list[int]:
    """The docs binder 1 lists, then mark 1 links, through *model*'s relations."""
    listed = [row.id for row in await writes.related(binder, LISTS[model])]
    (mark,) = await writes.rows(writes.m.Mark)
    linked = getattr(mark, f"{LINKS[model]}_id")
    return listed if linked is None else [*listed, linked]


class AbstractTestWriteLookupScopes:
    @pytest.mark.parametrize(("scenario", "entry", "lookup"), cells(SCENARIOS))
    async def test_a_doc_a_governing_repo_hides_reads_as_missing(
        self, writes, scenario, entry, lookup
    ):
        await look_up(writes, SCENARIOS[scenario], entry, lookup)


class AbstractTestSiblingLookupScopes:
    """Doc 2 is a public doc too, and the public docs' repo hides it."""

    @pytest.mark.parametrize(("scenario", "entry", "lookup"), cells(SIBLINGS))
    async def test_a_doc_a_siblings_repo_hides_reads_as_missing(
        self, writes, scenario, entry, lookup
    ):
        await look_up(writes, SIBLINGS[scenario], entry, lookup)
