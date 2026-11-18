"""A row two sibling subclasses hold is a row of both, whichever writes it.

A Django multi-table child, or a SQLAlchemy joined-table subclass without a
discriminator, is held apart by its own table, and two siblings' tables can
hold the same row: a place that is a restaurant and a bar at once. Written as
a restaurant, the row is still the bar's, so the bar's repo has its say too:
a row it hides reads as missing, and its ``can_*`` hooks and ``authorize`` are
asked about the row as about one of its own. A list of a subclass takes a row
from a row of its base just as from one of its own.

Tortoise has no such rows: a subclass keeps rows of its own in a table of its
own (see ``tests/backends/tortoise/test_write_path_subclass_rows.py``).

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over an
``SbGuide`` listing ``SbRestaurant`` rows, ``SbRestaurant`` and ``SbBar``
subclassing ``SbPlace``, ``SbReview`` rows each pointing at a restaurant, and
``SbPhoto`` rows each listed by a place; ``also_a(model, pk)`` makes an
existing row a row of another subclass as well.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo
from tests.abstract.write_path_authorization import item, member


@strawberry.input
class SbRestaurantUpdate:
    id: strawberry.ID
    name: str | None = strawberry.UNSET


@strawberry.input
class SbPhotoUpdate:
    id: strawberry.ID


def upsert_on(on_match: str) -> dict[str, Any]:
    return {"update": ["name"], "upsert": {"where": ["id"], "onMatch": on_match}}


GUIDE = {
    "_meta": upsert_on("UPDATE"),
    "restaurants": {"_meta": {"update": ["name"], "unlink": True, "delete": True}},
}
REVIEW = {
    "_meta": {
        "update": ["label"],
        "upsert": {"where": ["id"], "onMatch": "UPDATE"},
    },
    "restaurant": {"_meta": upsert_on("IGNORE")},
}

#: Each way to write restaurant 20, which is a bar as well: the bar's hooks
#: it asks in turn, the action ``authorize`` is asked, and whether a plain
#: ref-list item ``authorize`` denies is skipped rather than refused.
ENTRIES = {
    "upsert update": (["can_upsert", "can_update"], "upsert", False),
    "upsert ignore": (["can_upsert"], "upsert", False),
    "ref-list update": (["can_update", "can_link"], "update", True),
    "ref-list unlink": (["can_unlink"], "unlink", True),
    "ref-list delete": (["can_delete"], "delete", True),
    "nested update": (["can_update", "can_link"], "update", False),
    "nested unlink": (["can_unlink"], "unlink", False),
    "nested delete": (["can_delete"], "delete", False),
    "link target": (["can_update", "can_link"], "update", False),
    "re-point": (["can_unlink"], "unlink", False),
}


def sibling_repo(
    writes: Any, calls: list, *, visible: list[int] | None = None, refuses: bool = False
) -> type:
    """A repo showing the *visible* rows (all with ``None``) that records its hooks."""

    class Sibling(AbstractRepo):
        def scope_query(self, query, info):
            if visible is None:
                return query
            return writes.only(query, self.model, visible)

        def asked(self, hook: str, row: Any) -> bool:
            calls.append((hook, row.id))
            return not refuses

        def can_update(self, instance, data, info):
            return self.asked("can_update", instance)

        def can_upsert(self, instance, info):
            return self.asked("can_upsert", instance)

        def can_delete(self, instance, info):
            return self.asked("can_delete", instance)

        def can_link(self, parent, field, instance, info):
            return self.asked("can_link", instance)

        def can_unlink(self, parent, field, instance, info):
            return self.asked("can_unlink", parent if field == "photos" else instance)

    return Sibling


def denying(model: type, seen: list, action: str | None = None) -> Any:
    """An ``authorize`` denying *action* (any, with ``None``) on *model* alone."""

    def authorize(action_asked, model_asked, pk, info):
        seen.append((action_asked, model_asked.__name__, pk))
        return model_asked is not model or action not in (None, action_asked)

    return authorize


async def outcome(write: Any) -> tuple[type | None, str | None]:
    try:
        await write
    except (PermissionError, LookupError) as refused:
        return type(refused), str(refused)
    return None, None


class AbstractTestARowOfTwoSiblingsIsWrittenAsBoth:
    """Guide 1 lists restaurants 10, 20 and 30; restaurant 20 is a bar too.

    Review 1 points at restaurant 10 and review 2 at restaurant 20. Place 40
    is no restaurant, and photo 1 is listed by it.
    """

    @pytest_asyncio.fixture
    async def places(self, writes):
        m = writes.m
        guide = await writes.create(m.Guide, id=1, name="city")
        restaurants = {
            pk: await writes.create(m.Restaurant, id=pk, name=name, guide=guide)
            for pk, name in ((10, "diner"), (20, "pub"), (30, "cafe"))
        }
        await writes.also_a(m.Bar, 20)
        park = await writes.create(m.Place, id=40, name="park")
        await writes.create(m.Review, id=1, label="first", restaurant=restaurants[10])
        await writes.create(m.Review, id=2, label="second", restaurant=restaurants[20])
        await writes.create(m.Photo, id=1, label="view", place=park)
        return SimpleNamespace(guide=guide, diner=restaurants[10])

    @pytest.fixture
    def calls(self):
        return []

    @staticmethod
    def orm(writes: Any, calls: list, **sibling: Any) -> Any:
        repos = {writes.m.Bar: sibling_repo(writes, calls, **sibling)}
        return writes.new_orm(repos=repos)

    @staticmethod
    async def state(writes: Any) -> tuple[list, list]:
        m = writes.m
        restaurants = [
            (row.id, row.name, row.guide_id) for row in await writes.rows(m.Restaurant)
        ]
        reviews = [(row.id, row.restaurant_id) for row in await writes.rows(m.Review)]
        return restaurants, reviews

    @staticmethod
    async def write(
        writes: Any, orm: Any, places: Any, entry: str, pk: str, **check: Any
    ) -> None:
        """Write restaurant *pk* through *entry*.

        A link target is pointed at by review 1; a re-point moves the review
        pointing at *pk* to restaurant 30.
        """
        m = writes.m
        operation = entry.split()[-1]
        if entry.startswith("upsert"):
            on_match = "UPDATE" if operation == "update" else "IGNORE"
            upsert = orm.mutations.upsert_node_input(
                model=m.Restaurant,
                project={"_meta": upsert_on(on_match)},
                name=f"SbRestaurantUpsert{on_match.title()}",
            )
            payload = upsert(where=member(upsert, "where")(id=pk))
            if on_match == "UPDATE":
                payload.update = member(upsert, "update")(name="pwned")
            await writes.upsert(orm, payload, **check)
        elif entry.startswith("ref-list"):
            ref = orm.ref(
                m.Restaurant, update=SbRestaurantUpdate, unlink=True, delete=True
            )
            values = {"name": "pwned"} if operation == "update" else {}
            refs = [item(ref, operation, id=pk, **values)]
            await writes.refs(orm, places.guide, "restaurants", refs, **check)
        elif entry.startswith("nested"):
            upsert = orm.mutations.upsert_node_input(
                model=m.Guide, project=GUIDE, name="SbGuideUpsert"
            )
            items = member(upsert, "update", "restaurants")
            values = {"name": "pwned"} if operation == "update" else {}
            payload = upsert(
                where=member(upsert, "where")(id="1"),
                update=member(upsert, "update")(
                    restaurants=[item(items, operation, id=pk, **values)]
                ),
            )
            await writes.upsert(orm, payload, **check)
        else:
            upsert = orm.mutations.upsert_node_input(
                model=m.Review, project=REVIEW, name="SbReviewUpsert"
            )
            link = member(upsert, "update", "restaurant")
            review, target = (
                ("1", pk)
                if entry == "link target"
                else (
                    {"20": "2", "10": "1"}[pk],
                    "30",
                )
            )
            payload = upsert(
                where=member(upsert, "where")(id=review),
                update=member(upsert, "update")(
                    restaurant=link(update=member(link, "update")(id=target))
                ),
            )
            await writes.upsert(orm, payload, **check)

    @pytest.mark.parametrize("entry", ENTRIES)
    async def test_a_row_the_siblings_repo_hides_reads_as_missing(
        self, writes, places, calls, entry
    ):
        orm = self.orm(writes, calls, visible=[])
        before = await self.state(writes)
        hidden = await outcome(self.write(writes, orm, places, entry, "20"))
        if entry == "re-point":
            expected = (PermissionError, "The row the link held cannot be let go of")
        else:
            refused, message = await outcome(
                self.write(writes, orm, places, entry, "99")
            )
            expected = (refused, message and message.replace("99", "20"))
        assert hidden == expected, f"refused as a missing row is: {hidden}"
        assert calls == [], f"no hook is asked about the hidden row: {calls}"
        assert await self.state(writes) == before, "nothing is written"

    @pytest.mark.parametrize("entry", ENTRIES)
    async def test_the_siblings_hooks_are_asked(self, writes, places, calls, entry):
        hook = ENTRIES[entry][0][0]
        orm = self.orm(writes, calls, refuses=True)
        before = await self.state(writes)
        with pytest.raises(PermissionError, match=f"{hook} denied"):
            await self.write(writes, orm, places, entry, "20")
        assert calls == [(hook, 20)], f"the bar's repo is asked {hook}: {calls}"
        assert await self.state(writes) == before, "nothing is written"

    @pytest.mark.parametrize("entry", ENTRIES)
    async def test_authorize_is_asked_about_the_sibling(
        self, writes, places, calls, entry
    ):
        _hooks, action, skips = ENTRIES[entry]
        seen: list = []
        check = {"authorize": denying(writes.m.Bar, seen)}
        orm = writes.new_orm()
        before = await self.state(writes)
        if skips:
            await self.write(writes, orm, places, entry, "20", **check)
        else:
            with pytest.raises(PermissionError, match=f"authorize denied the {action}"):
                await self.write(writes, orm, places, entry, "20", **check)
        asked = (action, writes.m.Bar.__name__, 20)
        assert asked in seen, f"authorize is asked {asked}, not only {seen}"
        assert await self.state(writes) == before, "nothing is written"

    @pytest.mark.parametrize("entry", ENTRIES)
    async def test_an_allowed_write_asks_the_sibling(
        self, writes, places, calls, entry
    ):
        before = await self.state(writes)
        await self.write(writes, self.orm(writes, calls), places, entry, "20")
        expected = [(hook, 20) for hook in ENTRIES[entry][0]]
        assert calls == expected, f"the bar's repo is asked in turn: {calls}"
        changed = await self.state(writes) != before
        assert changed is (entry != "upsert ignore"), "the write goes through"

    @pytest.mark.parametrize("entry", ENTRIES)
    async def test_a_row_of_one_subclass_asks_the_other_nothing(
        self, writes, places, calls, entry
    ):
        """Restaurants 10 and 30 are no bars, so a bar's repo has no say on them."""
        seen: list = []
        check = {"authorize": denying(writes.m.Bar, seen)}
        orm = self.orm(writes, calls, visible=[], refuses=True)
        pk = "10" if entry == "re-point" else "30"
        await self.write(writes, orm, places, entry, pk, **check)
        assert calls == [], f"the bar's repo is asked nothing: {calls}"
        bar = writes.m.Bar.__name__
        assert all(model != bar for _a, model, _pk in seen), seen

    @pytest.mark.parametrize("refusal", ["hidden", "can_unlink", "authorize", None])
    async def test_a_subclass_list_takes_a_row_from_a_row_of_its_base(
        self, writes, places, calls, refusal
    ):
        """Photo 1 moves to the diner's photos off the park, a plain place."""
        m = writes.m
        seen: list = []
        holder = sibling_repo(
            writes,
            calls,
            visible=[10, 20, 30] if refusal == "hidden" else None,
            refuses=refusal == "can_unlink",
        )
        orm = writes.new_orm(repos={m.Place: holder})
        denied = "unlink" if refusal == "authorize" else "nothing"
        check = {"authorize": denying(m.Place, seen, denied)}
        ref = orm.ref(m.Photo, update=SbPhotoUpdate)
        refs = [item(ref, "update", id="1")]
        write = writes.refs(orm, places.diner, "photos", refs, **check)
        _refused, message = await outcome(write)
        expected = {
            "hidden": "The row cannot be taken from the row that holds it",
            "can_unlink": "AbstractRepo.can_unlink denied the operation",
            "authorize": "authorize denied the unlink operation",
        }.get(refusal)
        assert message == expected, f"refused as {expected}: {message}"
        asked = [] if refusal in ("hidden", "authorize") else [("can_unlink", 40)]
        assert calls == asked, f"the park's repo is asked {asked}: {calls}"
        assert ("unlink", m.Place.__name__, 40) in seen or refusal == "hidden", seen
        photos = [photo.place_id for photo in await writes.rows(m.Photo)]
        assert photos == [40 if refusal else 10], f"the photo is on {photos}"
