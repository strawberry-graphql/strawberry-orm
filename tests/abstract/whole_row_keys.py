"""A row is told apart from every other by its whole primary key.

A key can be several columns. Wherever the library matches rows by key -
loading relations onto rows an app holds, checking rows a resolver returns
against ``scope_rows``, filing a page's rows into their groups, finding the row
an upsert names - one of those columns alone takes in every row that shares
it, and hands one row another's data, or a row its scope hides.

Each backend supplies a ``keyed`` fixture: a :class:`KeyedRows` over one shape
of its models - a key of two columns, of a foreign key and a column, of one
column - in each mode it reads in. Every shape holds the same rows: tags
``plain``, ``hush`` and ``other``, and lines ``shown`` (tagged plain, with the
note "first"), ``secret`` (tagged hush, with the note "second") and ``other``
(tagged other). Under a key of several columns, ``shown`` and ``secret`` share
the first. A scoped line type, and a scoped repo, hide ``secret``.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import (
    AbstractRepo,
    UpsertNotFoundError,
    UpsertValidationError,
    filter_field,
)
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import fields_of, member

SHOWN, SECRET, OTHER = "shown", "secret", "other"
TAGS = {SHOWN: "plain", SECRET: "hush", OTHER: "other"}
NOTES = {SHOWN: ["first"], SECRET: ["second"], OTHER: []}


async def resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass
class KeyedRows:
    """One backend's shape of keyed rows, as the tests below drive it."""

    #: ``Tag``, ``Line`` and ``Note``.
    m: SimpleNamespace
    #: Each line's primary key, as its rows report it, by label.
    keys: dict[str, Any]
    #: The id a write names each line by, or ``None`` where an id cannot.
    ids: dict[str, str] | None
    #: An id naming only the first column of a key of several, else ``None``.
    part_id: str | None
    #: Every attribute holding part of a line's key.
    key_names: set[str]
    #: A key attribute, and the value moving ``shown`` onto ``secret``'s key.
    moved: tuple[str, Any]

    @property
    def context(self) -> dict[str, Any]:
        return {}

    @property
    def info(self) -> Any:
        return SimpleNamespace(context=self.context)

    async def run(self, schema: Any, query: str, variables: Any = None) -> Any:
        return await schema.execute(
            query, variable_values=variables, context_value=self.context
        )

    async def write(self, call: Callable[[], Any]) -> Any:
        """Run a library write the way this mode's resolvers would."""
        return await resolve(call())

    # -- Per backend -----------------------------------------------------------

    def orm(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    def hide_secret(self, query: Any) -> Any:
        """*query* over lines without ``secret``, kept a subquery where it can be."""
        raise NotImplementedError

    def is_first(self, query: Any, value: bool) -> Any:
        """*query* over the lines whose last key column is 1, or the others."""
        raise NotImplementedError

    def notes_of(self, line: Any) -> Any:
        """An unexecuted query for *line*'s notes, matched on its whole key."""
        raise NotImplementedError

    async def held(self) -> dict[str, Any]:
        """Every line, by label, read the way an app holding them would."""
        raise NotImplementedError

    async def table(self) -> dict[Any, str]:
        """Every line's label, by its key."""
        raise NotImplementedError


async def execute(keyed: KeyedRows, schema: Any, query: str, variables=None) -> Any:
    result = await keyed.run(schema, query, variables)
    assert result.errors is None, f"{query} failed: {result.errors}"
    return result.data


def scoped_line_type(orm: Any, keyed: KeyedRows, **kwargs: Any) -> type:
    hide = keyed.hide_secret

    @orm.type(keyed.m.Line, **kwargs)
    class LineType:
        label: auto

        @classmethod
        def scope_rows(cls, query, info):
            return hide(query)

    return LineType


def labels(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["label"] for row in rows)


class AbstractTestReadsTellRowsApart:
    async def test_relations_loaded_onto_held_rows_are_their_own(self, keyed):
        orm, m = keyed.orm(), keyed.m
        held = await keyed.held()

        @orm.type(m.Tag)
        class TagType:
            name: auto

        @orm.type(m.Line)
        class LineType:
            label: auto
            tag: TagType

        @strawberry.type
        class Query:
            @strawberry.field
            def held(self) -> list[LineType]:
                return [held[SHOWN], held[OTHER]]

        data = await execute(
            keyed, orm.schema(query=Query), "{ held { label tag { name } } }"
        )
        found = {line["label"]: line["tag"]["name"] for line in data["held"]}
        assert found == {SHOWN: "plain", OTHER: "other"}, (
            f"each held line should carry its own tag, got {found}"
        )

    async def test_rows_a_resolver_returns_are_held_to_the_scope(self, keyed):
        orm = keyed.orm()
        held = await keyed.held()
        LineType = scoped_line_type(orm, keyed)

        @strawberry.type
        class Query:
            @strawberry.field
            def held(self) -> list[LineType]:
                return list(held.values())

        data = await execute(keyed, orm.schema(query=Query), "{ held { label } }")
        assert labels(data["held"]) == [OTHER, SHOWN], (
            f"the scope should drop the secret line alone, got {data}"
        )

    async def test_a_node_is_held_to_the_scope(self, keyed):
        orm, m = keyed.orm(), keyed.m
        held = await keyed.held()
        hide = keyed.hide_secret

        @orm.type(m.Line)
        class LineNode(relay.Node):
            label: relay.NodeID[str]

            @classmethod
            def scope_rows(cls, query, info):
                return hide(query)

            @classmethod
            def resolve_nodes(cls, *, info, node_ids, required=False):
                return [held.get(node_id) for node_id in node_ids]

        @strawberry.type
        class Query:
            node: relay.Node | None = relay.node()

        ids = {label: str(relay.GlobalID("LineNode", label)) for label in TAGS}
        nodes = " ".join(
            f'{label}: node(id: "{ids[label]}") {{ id }}' for label in (SHOWN, SECRET)
        )
        query = f"{{ {nodes} }}"
        data = await execute(keyed, orm.schema(query=Query, types=[LineNode]), query)
        assert data == {SHOWN: {"id": ids[SHOWN]}, SECRET: None}, (
            f"the secret node should be hidden and the shown one kept, got {data}"
        )

    async def test_each_group_holds_its_own_rows(self, keyed):
        orm, m = keyed.orm(), keyed.m

        @orm.type(m.Tag, group=orm.group(m.Tag))
        class TagType:
            name: auto

        @orm.type(m.Line, group=orm.group(m.Line))
        class LineType:
            label: auto
            tag: TagType

        @strawberry.type
        class Query:
            line_page: ORMListConnection[LineType] = orm.connection.eager()

        data = await execute(
            keyed,
            orm.schema(query=Query),
            "{ linePage(groupBy: [{object: {tag: {field: {name: true}}}}]) "
            "{ edges { node { label } } groups { key { tag { name } } "
            "edgeIndices aggregates { count } items { edges { node { label } } } } } }",
        )
        page = data["linePage"]
        shown = [edge["node"]["label"] for edge in page["edges"]]
        found = {
            group["key"]["tag"]["name"]: (
                sorted(shown[index] for index in group["edgeIndices"]),
                labels([edge["node"] for edge in group["items"]["edges"]]),
                group["aggregates"]["count"],
            )
            for group in page["groups"]
        }
        assert found == {
            "plain": ([SHOWN], [SHOWN], 1),
            "hush": ([SECRET], [SECRET], 1),
            "other": ([OTHER], [OTHER], 1),
        }, f"each tag's group should hold its own line alone, got {found}"

    async def test_rows_read_for_each_parent_are_its_own(self, keyed):
        orm, m = keyed.orm(), keyed.m
        notes_of = keyed.notes_of

        @orm.type(m.Note)
        class NoteType:
            text: auto

        @orm.type(m.Line)
        class LineType:
            label: auto

            @orm.field.lazy
            def notes(self) -> list[NoteType]:
                return notes_of(self)

        @strawberry.type
        class Query:
            lines: list[LineType] = orm.field.eager()

        data = await execute(
            keyed, orm.schema(query=Query), "{ lines { label notes { text } } }"
        )
        found = {
            line["label"]: [note["text"] for note in line["notes"]]
            for line in data["lines"]
        }
        assert found == NOTES, f"each line should get its own notes, got {found}"

    async def test_a_filter_through_a_scoped_relation_skips_hidden_rows(self, keyed):
        orm, m = keyed.orm(), keyed.m
        LineType = scoped_line_type(orm, keyed, filters=orm.filter(m.Line))

        @orm.type(m.Tag, filters=orm.filter(m.Tag))
        class TagType:
            name: auto
            lines: list[LineType]

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field.eager()

        schema = orm.schema(query=Query)
        found = {}
        for label in (SHOWN, SECRET):
            data = await execute(
                keyed,
                schema,
                "{ tags(filter: {object: {lines: {field: "
                f'{{label: {{exact: "{label}"}}}}}}}}}}) {{ name }} }}',
            )
            found[label] = sorted(tag["name"] for tag in data["tags"])
        data = await execute(
            keyed,
            schema,
            "{ tags(filter: {object: {lines: {isNull: false}}}) { name } }",
        )
        found["any"] = sorted(tag["name"] for tag in data["tags"])
        assert found == {SHOWN: ["plain"], SECRET: [], "any": ["other", "plain"]}, (
            f"only the tag of a line the scope shows should match, got {found}"
        )

    async def test_paging_shows_every_row_once(self, keyed):
        orm = keyed.orm()

        @orm.type(keyed.m.Line)
        class LineType:
            label: auto

        @strawberry.type
        class Query:
            line_page: ORMListConnection[LineType] = orm.connection.eager()

        schema = orm.schema(query=Query)
        seen, after = [], None
        for _ in range(4):
            data = await execute(
                keyed,
                schema,
                "query ($after: String) { linePage(first: 1, after: $after) "
                "{ edges { node { label } } pageInfo { hasNextPage endCursor } } }",
                {"after": after},
            )
            page = data["linePage"]
            seen += [edge["node"]["label"] for edge in page["edges"]]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        assert sorted(seen) == [OTHER, SECRET, SHOWN], (
            f"the pages should hold every line once, got {seen}"
        )

    async def test_a_custom_filter_below_the_top_matches_its_own_rows(self, keyed):
        orm, m = keyed.orm(), keyed.m
        is_first = keyed.is_first

        @orm.filter_type(m.Line)
        class LineFilter:
            label: auto

            @filter_field
            def first(self, value: bool, query):
                return is_first(query, value)

        @orm.type(m.Line, filters=LineFilter)
        class LineType:
            label: auto

        @strawberry.type
        class Query:
            lines: list[LineType] = orm.field.eager()

        schema = orm.schema(query=Query)
        found = {}
        for condition in ("not: {first: true}", "any: [{first: false}]"):
            data = await execute(
                keyed, schema, f"{{ lines(filter: {{{condition}}}) {{ label }} }}"
            )
            found[condition] = labels(data["lines"])
        assert found == {
            "not: {first: true}": [SECRET],
            "any: [{first: false}]": [SECRET],
        }, f"each condition should keep the secret line alone, got {found}"

    async def test_a_parents_connection_counts_its_own_rows(self, keyed):
        orm, m = keyed.orm(), keyed.m
        LineType = scoped_line_type(orm, keyed)

        @orm.type(m.Tag)
        class TagType:
            name: auto
            lines: ORMListConnection[LineType] = orm.connection.eager()

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field.eager()

        data = await execute(
            keyed,
            orm.schema(query=Query),
            "{ tags { name lines { totalCount edges { node { label } } } } }",
        )
        found = {
            tag["name"]: (
                tag["lines"]["totalCount"],
                [edge["node"]["label"] for edge in tag["lines"]["edges"]],
            )
            for tag in data["tags"]
        }
        assert found == {
            "plain": (1, [SHOWN]),
            "hush": (0, []),
            "other": (1, [OTHER]),
        }, f"each tag should count and page its own visible lines, got {found}"

    async def test_a_parents_list_holds_its_own_rows(self, keyed):
        orm, m = keyed.orm(), keyed.m
        LineType = scoped_line_type(orm, keyed)

        @orm.type(m.Tag)
        class TagType:
            name: auto
            lines: list[LineType]

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field.eager()

        data = await execute(
            keyed, orm.schema(query=Query), "{ tags { name lines { label } } }"
        )
        found = {tag["name"]: labels(tag["lines"]) for tag in data["tags"]}
        assert found == {"plain": [SHOWN], "hush": [], "other": [OTHER]}, (
            f"each tag should hold its own visible lines, got {found}"
        )


def hiding_repo(keyed: KeyedRows, **hooks: Callable[..., Any]) -> type:
    hide = keyed.hide_secret

    class HideSecret(AbstractRepo):
        def scope_query(self, query, info):
            return hide(query)

    for name, hook in hooks.items():
        setattr(HideSecret, name, hook)
    return HideSecret


class _Writes:
    def writer(self, keyed: KeyedRows, where: str, **hooks: Callable[..., Any]):
        orm = keyed.orm(repos={keyed.m.Line: hiding_repo(keyed, **hooks)})

        @orm.type(keyed.m.Line)
        class LineType:
            label: auto

        upsert = orm.mutations.upsert_node_input(
            model=keyed.m.Line,
            project={
                "_meta": {
                    "create": True,
                    "update": ["label"],
                    "upsert": {"where": [where], "onMatch": "UPDATE"},
                }
            },
            name=f"LineBy{where.title()}",
        )

        async def write(matched: Any, label: str) -> Any:
            payload = upsert(
                where=member(upsert, "where")(**{where: matched}),
                update=member(upsert, "update")(label=label),
            )
            return await keyed.write(lambda: orm.apply_upsert(payload, keyed.info))

        return orm, upsert, write

    async def assert_lines(self, keyed: KeyedRows, **changed: str) -> None:
        expected = {key: changed.get(label, label) for label, key in keyed.keys.items()}
        found = await keyed.table()
        assert found == expected, f"the lines should read {expected}, got {found}"


class AbstractTestWritesTellRowsApart(_Writes):
    async def test_an_upsert_updates_the_row_its_where_matched(self, keyed):
        _orm, _upsert, write = self.writer(keyed, "label")
        await write(SHOWN, "seen")
        await self.assert_lines(keyed, shown="seen")

    async def test_an_upsert_skips_a_row_its_repo_hides(self, keyed):
        _orm, _upsert, write = self.writer(keyed, "label")
        with pytest.raises(UpsertNotFoundError):
            await write(SECRET, "seen")
        await self.assert_lines(keyed)

    async def test_an_upsert_by_id_updates_that_row_alone(self, keyed):
        _orm, _upsert, write = self.writer(keyed, "id")
        if keyed.ids is None:
            with pytest.raises(UpsertValidationError, match="more than one column"):
                await write("1", "seen")
            await self.assert_lines(keyed)
            return
        await write(keyed.ids[SHOWN], "seen")
        await self.assert_lines(keyed, shown="seen")

    async def test_an_upsert_by_id_skips_a_row_its_repo_hides(self, keyed):
        _orm, _upsert, write = self.writer(keyed, "id")
        refused = UpsertValidationError if keyed.ids is None else UpsertNotFoundError
        with pytest.raises(refused):
            await write((keyed.ids or {}).get(SECRET, "1"), "seen")
        await self.assert_lines(keyed)

    async def test_an_id_naming_part_of_a_key_is_refused(self, keyed):
        if keyed.part_id is None:
            pytest.skip("a key of one column has no part to name")
        _orm, _upsert, write = self.writer(keyed, "id")
        with pytest.raises(UpsertValidationError):
            await write(keyed.part_id, "seen")
        await self.assert_lines(keyed)

    async def test_an_update_cannot_move_a_row_onto_another_key(self, keyed):
        name, value = keyed.moved

        def on_before_update(self, instance, data, info):
            return {**data, name: value}

        _orm, _upsert, write = self.writer(
            keyed, "label", on_before_update=on_before_update
        )
        with pytest.raises(UpsertValidationError, match="cannot set"):
            await write(SHOWN, "moved")
        await self.assert_lines(keyed)

    async def test_generated_inputs_offer_no_key_column(self, keyed):
        orm, upsert, _write = self.writer(keyed, "label")
        offered = {
            "input": fields_of(orm.input(keyed.m.Line, name="LineKeyInput")),
            "create": fields_of(member(upsert, "create")),
            "update": fields_of(member(upsert, "update")),
        }
        named = {name: keys & keyed.key_names for name, keys in offered.items()}
        named["relation"] = set()
        meta = {
            "update": ["label"],
            "upsert": {"where": ["label"], "onMatch": "UPDATE"},
        }
        for name in sorted(keyed.key_names):
            try:
                orm.mutations.upsert_node_input(
                    model=keyed.m.Line,
                    project={"_meta": meta, name: {}},
                    name=f"LineVia{name}",
                )
            except ValueError as error:
                if "Unknown relation" in str(error):
                    continue
                raise
            named["relation"].add(name)
        expected = {"input": set(), "create": set(), "update": set(), "relation": set()}
        assert named == expected, (
            f"no generated input should offer a key column, got {named}"
        )
