"""A row a write takes from another row is let go of by that row as well.

Moving a comment onto a post takes it off the post it was on, and re-pointing
the comment's post does the same from the other end: either way the old post
loses a row from its list. So the old post is checked as a row the write
changes: read through its repos, where one hiding it refuses the move as if
it were missing, asked ``can_unlink``, and ``authorize`` is asked to
``unlink`` it. The same goes for a post moved between authors.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over its blog
models (see :class:`~tests.abstract.write_path_authorization.Blog`).
"""

from types import SimpleNamespace
from typing import Any

import pytest
import strawberry

from strawberry_orm import AbstractRepo
from strawberry_orm.mutations import RelationRemovalPolicy
from tests.abstract.write_path_authorization import Blog, item, label, member


@strawberry.input
class TkCommentUpdate:
    id: strawberry.ID
    body: str | None = strawberry.UNSET


@strawberry.input
class TkPostUpdate:
    id: strawberry.ID
    title: str | None = strawberry.UNSET


#: Comment 2 leaves Other for Hello; post 2, Other, leaves Bob for Alice.
RELATIONS = {
    "comments": SimpleNamespace(
        parent="Post",
        child="Comment",
        link="post",
        parent_field="title",
        child_field="body",
        update=TkCommentUpdate,
        old="Other",
        moved="theirs",
        staying="1",
    ),
    "posts": SimpleNamespace(
        parent="User",
        child="Post",
        link="author",
        parent_field="name",
        child_field="title",
        update=TkPostUpdate,
        old="Bob",
        moved="Other",
        staying="1",
    ),
}

MOVES = [
    ("comments", "ref-list update"),
    ("comments", "nested update"),
    ("comments", "re-point"),
    ("posts", "ref-list update"),
    ("posts", "nested update"),
    ("posts", "re-point"),
    ("posts", "re-point deleting"),
]
STAYS = [
    (relation, entry)
    for relation in RELATIONS
    for entry in ("ref-list update", "nested update", "re-point")
]


def holder_repo(
    writes: Any, calls: list, *, visible: list[int] | None = None, refuses: bool = False
) -> type:
    """The old parent's repo: it shows the *visible* rows and records ``can_unlink``."""

    class Holders(AbstractRepo):
        def scope_query(self, query, info):
            if visible is None:
                return query
            return writes.only(query, self.model, visible)

        def can_unlink(self, parent, field, instance, info):
            calls.append(("can_unlink", label(parent), field, label(instance)))
            return not refuses

    return Holders


def recording(seen: list, deny: tuple[str, type] | None = None) -> Any:
    def authorize(action, model, pk, info):
        seen.append((action, model.__name__, pk))
        return (action, model) != deny

    return authorize


class AbstractTestARowIsLetGoOfByTheRowItLeaves(Blog):
    @pytest.fixture
    def calls(self):
        return []

    @staticmethod
    async def move(
        writes: Any, orm: Any, blog: Any, relation: str, entry: str, pk: str, **check
    ) -> None:
        """Move child *pk* under the new parent (pk 1) through *entry*."""
        rel = RELATIONS[relation]
        parent, child = getattr(writes.m, rel.parent), getattr(writes.m, rel.child)
        if entry == "ref-list update":
            ref = orm.ref(child, update=rel.update)
            new_parent = blog.hello if relation == "comments" else blog.alice
            refs = [item(ref, "update", id=pk)]
            await writes.refs(orm, new_parent, relation, refs, **check)
            return
        if entry == "nested update":
            upsert = orm.mutations.upsert_node_input(
                model=parent,
                project={
                    "_meta": {
                        "update": [rel.parent_field],
                        "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                    },
                    relation: {"_meta": {"update": [rel.child_field]}},
                },
                name="TkParentUpsert",
            )
            items = member(upsert, "update", relation)
            payload = upsert(
                where=member(upsert, "where")(id="1"),
                update=member(upsert, "update")(
                    **{relation: [item(items, "update", id=pk)]}
                ),
            )
            await writes.upsert(orm, payload, **check)
            return
        upsert = orm.mutations.upsert_node_input(
            model=child,
            project={
                "_meta": {
                    "update": [rel.child_field],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                rel.link: {
                    "_meta": {
                        "update": [rel.parent_field],
                        "upsert": {"where": ["id"], "onMatch": "IGNORE"},
                        "onReplace": ["DISCONNECT", "DELETE"],
                    }
                },
            },
            name="TkChildUpsert",
        )
        link = member(upsert, "update", rel.link)
        sent = link(update=member(link, "update")(id="1"))
        if entry == "re-point deleting":
            sent.on_replace = RelationRemovalPolicy.DELETE
        payload = upsert(
            where=member(upsert, "where")(id=pk),
            update=member(upsert, "update")(**{rel.link: sent}),
        )
        await writes.upsert(orm, payload, **check)

    @staticmethod
    async def held_by(writes: Any, relation: str) -> int:
        """Which parent the child that moves is under."""
        rel = RELATIONS[relation]
        rows = await writes.rows(getattr(writes.m, rel.child))
        return getattr(rows[1], f"{rel.link}_id")

    @staticmethod
    def asked(relation: str, entry: str) -> tuple[str, str, str, str]:
        """The ``can_unlink`` the old parent's repo is asked."""
        rel = RELATIONS[relation]
        if entry.startswith("re-point"):
            return ("can_unlink", rel.moved, rel.link, rel.old)
        return ("can_unlink", rel.old, relation, rel.moved)

    @pytest.mark.parametrize(("relation", "entry"), MOVES)
    async def test_an_old_parent_the_repo_hides_reads_as_missing(
        self, writes, blog, calls, relation, entry
    ):
        rel = RELATIONS[relation]
        holders = holder_repo(writes, calls, visible=[1, 3])
        orm = writes.new_orm(repos={getattr(writes.m, rel.parent): holders})
        with pytest.raises(PermissionError) as refused:
            await self.move(writes, orm, blog, relation, entry, "2")
        expected = (
            "The row the link held cannot be let go of"
            if entry.startswith("re-point")
            else "The row cannot be taken from the row that holds it"
        )
        assert str(refused.value) == expected, str(refused.value)
        assert calls == [], f"no hook is asked about the hidden row: {calls}"
        assert await self.held_by(writes, relation) == 2, "the row stays where it was"

    @pytest.mark.parametrize(("relation", "entry"), MOVES)
    async def test_an_old_parent_is_asked_can_unlink(
        self, writes, blog, calls, relation, entry
    ):
        rel = RELATIONS[relation]
        holders = holder_repo(writes, calls, refuses=True)
        orm = writes.new_orm(repos={getattr(writes.m, rel.parent): holders})
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await self.move(writes, orm, blog, relation, entry, "2")
        expected = [self.asked(relation, entry)]
        assert calls == expected, f"the old parent is asked to let go: {calls}"
        assert await self.held_by(writes, relation) == 2, "the row stays where it was"

    @pytest.mark.parametrize(("relation", "entry"), MOVES)
    async def test_authorize_is_asked_to_unlink_the_old_parent(
        self, writes, blog, relation, entry
    ):
        rel = RELATIONS[relation]
        seen: list = []
        old_parent = getattr(writes.m, rel.parent)
        authorize = recording(seen, deny=("unlink", old_parent))
        orm = writes.new_orm()
        with pytest.raises(PermissionError, match="authorize denied the unlink"):
            await self.move(
                writes, orm, blog, relation, entry, "2", authorize=authorize
            )
        asked = ("unlink", old_parent.__name__, 2)
        assert asked in seen, f"authorize is asked {asked}, not only {seen}"
        assert await self.held_by(writes, relation) == 2, "the row stays where it was"

    @pytest.mark.parametrize(("relation", "entry"), MOVES)
    async def test_an_allowed_move_asks_the_old_parent_and_moves(
        self, writes, blog, calls, relation, entry
    ):
        rel = RELATIONS[relation]
        seen: list = []
        old_parent = getattr(writes.m, rel.parent)
        orm = writes.new_orm(repos={old_parent: holder_repo(writes, calls)})
        await self.move(
            writes, orm, blog, relation, entry, "2", authorize=recording(seen)
        )
        assert calls == [self.asked(relation, entry)], f"asked: {calls}"
        asked = ("unlink", old_parent.__name__, 2)
        assert asked in seen, f"authorize is asked {asked}, not only {seen}"
        assert await self.held_by(writes, relation) == 1, "the row moves"
        if entry == "re-point deleting":
            users = [user.name for user in await writes.rows(writes.m.User)]
            assert users == ["Alice", "Carol"], f"the old author goes: {users}"

    @pytest.mark.parametrize(("relation", "entry"), STAYS)
    async def test_a_row_staying_where_it_is_asks_its_parent_nothing(
        self, writes, blog, calls, relation, entry
    ):
        rel = RELATIONS[relation]
        seen: list = []
        parent = getattr(writes.m, rel.parent)
        authorize = recording(seen, deny=("unlink", parent))
        orm = writes.new_orm(repos={parent: holder_repo(writes, calls, refuses=True)})
        await self.move(
            writes, orm, blog, relation, entry, rel.staying, authorize=authorize
        )
        assert calls == [], f"its parent's repo is asked nothing: {calls}"
        assert all(action != "unlink" for action, *_ in seen), seen
