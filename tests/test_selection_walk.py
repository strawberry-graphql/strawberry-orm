"""Walking a selection expands each fragment once, however often it is spread.

A query that spreads a fragment twice at every level selects, expanded spread
by spread, exponentially many fields in its depth - all of them the same few
fields. These walks must cost what the query's text does.
"""

from types import SimpleNamespace

import strawberry
from graphql import parse

from strawberry_orm.backends._base import _selected_fields
from strawberry_orm.optimizer.selections import (
    field_nodes_from_info,
    iter_field_nodes,
    merged_field_nodes,
    narrow_info,
    selected_fields,
    selects_anywhere,
)
from strawberry_orm.relay.connection import _edges_answered

#: Expanded spread by spread, a query this deep selects 2**DEPTH fields.
DEPTH = 12


class _Lookups(dict):
    """Fragments by name, counting the lookups a walk makes."""

    count = 0

    def get(self, name, default=None):
        self.count += 1
        return super().get(name, default)


def _parse(document: str):
    doc = parse(document)
    fragments = {
        defn.name.value: defn
        for defn in doc.definitions
        if defn.kind == "fragment_definition"
    }
    operation = next(
        defn for defn in doc.definitions if defn.kind == "operation_definition"
    )
    return operation.selection_set.selections[0], fragments


def _info(document: str, **variables):
    node, fragments = _parse(document)
    return SimpleNamespace(
        field_nodes=[node], fragments=fragments, variable_values=variables
    )


def _spread_twice_through_fragments(depth: int) -> str:
    fragments = "\n".join(
        f"fragment F{n} on T {{ ...F{n + 1} ...F{n + 1} }}" for n in range(depth)
    )
    return f"{fragments}\nfragment F{depth} on T {{ id }}\n{{ items {{ ...F0 }} }}"


def _spread_twice_through_fields(depth: int) -> str:
    fragments = "\n".join(
        f"fragment F{n} on T {{ a {{ ...F{n + 1} }} b {{ ...F{n + 1} }} }}"
        for n in range(depth)
    )
    return f"{fragments}\nfragment F{depth} on T {{ id }}\n{{ items {{ ...F0 }} }}"


class TestIterFieldNodes:
    def test_a_fragment_spread_twice_yields_its_fields_once(self):
        node, fragments = _parse(
            "fragment F on T { id name } { items { ...F id ...F } }"
        )
        names = [n.name.value for n in iter_field_nodes(node.selection_set, fragments)]
        assert names == ["id", "name", "id"]

    def test_fragments_spreading_fragments_are_expanded_once_each(self):
        node, fragments = _parse(_spread_twice_through_fragments(DEPTH))
        names = [n.name.value for n in iter_field_nodes(node.selection_set, fragments)]
        assert names == ["id"]


class TestMergedFieldNodes:
    def test_the_fields_of_one_name_are_grouped_aliases_and_all(self):
        node, fragments = _parse(
            """
            fragment F on User { posts { body } name }
            { users { posts { title } latest: posts(first: 1) { id } ...F } }
            """
        )
        merged = merged_field_nodes([node.selection_set], fragments)
        assert list(merged) == ["posts", "name"]
        assert [n.alias.value if n.alias else None for n in merged["posts"]] == [
            None,
            "latest",
            None,
        ]

    def test_a_fragment_spread_in_each_of_the_merged_fields_is_expanded_once(self):
        users, fragments = _parse(
            """
            fragment F on Post { title }
            { users { posts { ...F } drafts: posts { ...F } } }
            """
        )
        below = [n.selection_set for n in users.selection_set.selections]
        merged = merged_field_nodes(below, fragments)
        assert {name: len(nodes) for name, nodes in merged.items()} == {"title": 1}

    def test_wrappers_are_looked_through(self):
        node, _ = _parse("{ users { edges { node { name } } } }")
        assert list(merged_field_nodes([node.selection_set])) == ["name"]


class TestNarrowInfo:
    def test_a_fragment_spread_twice_narrows_to_its_field_once(self):
        info = _info(
            """
            fragment P on Payload { data { id } }
            { payload { ...P ...P } }
            """
        )
        narrowed = field_nodes_from_info(narrow_info(info, "data"))
        assert [n.name.value for n in narrowed] == ["data"]


class TestSelectsAnywhere:
    names = frozenset({"edges", "pageInfo"})

    def test_finds_a_field_however_deep(self):
        info = _info("{ conn { totalCount wrap { pageInfo { hasNextPage } } } }")
        assert selects_anywhere(info, self.names)

    def test_finds_a_field_in_an_inline_fragment(self):
        info = _info("{ conn { ... on C { edges { cursor } } } }")
        assert selects_anywhere(info, self.names)

    def test_looks_past_an_inline_fragment_without_it(self):
        info = _info("{ conn { ... on C { totalCount } } }")
        assert not selects_anywhere(info, self.names)

    def test_finds_a_field_in_a_fragment(self):
        info = _info("fragment F on C { edges { cursor } } { conn { ...F } }")
        assert selects_anywhere(info, self.names)

    def test_finds_a_spread_named_like_the_field(self):
        info = _info("fragment edges on C { totalCount } { conn { ...edges } }")
        assert selects_anywhere(info, self.names)

    def test_an_unknown_fragment_selects_nothing(self):
        assert not selects_anywhere(_info("{ conn { ...Missing } }"), self.names)

    def test_searches_each_fragment_once(self):
        info = _info(_spread_twice_through_fields(DEPTH))
        info.fragments = _Lookups(info.fragments)
        assert not selects_anywhere(info, self.names)
        assert info.fragments.count == DEPTH + 1

    def test_reads_the_raw_info_behind_strawberrys(self):
        raw = _info("fragment F on C { edges { cursor } } { conn { ...F } }")
        assert selects_anywhere(SimpleNamespace(_raw_info=raw), self.names)

    def test_no_field_selects_nothing(self):
        info = SimpleNamespace(field_nodes=[], fragments={})
        assert not selects_anywhere(info, self.names)


def _shape(fields) -> list:
    return [
        (field.name, field.arguments, _shape(_selected_fields(field)))
        for field in fields
    ]


class TestSelectedFields:
    def test_reads_names_arguments_and_selections_off_the_query(self):
        info = _info(
            """
            query ($n: Int) { users(first: $n) { name ...F ...F } }
            fragment F on User { posts(first: 2, order: {title: ASC}) { title } }
            """,
            n=3,
        )
        assert _shape(selected_fields(info)) == [
            (
                "users",
                {"first": 3},
                [
                    ("name", {}, []),
                    (
                        "posts",
                        {"first": "2", "order": {"title": "ASC"}},
                        [("title", {}, [])],
                    ),
                ],
            )
        ]

    def test_is_what_strawberry_selects_for_a_query_without_repeats(self):
        seen = {}

        @strawberry.type
        class Post:
            title: str = "t"

        @strawberry.type
        class User:
            name: str = "n"

            @strawberry.field
            def posts(self, first: int = 0) -> list[Post]:
                return [Post()]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.Info, first: int = 0) -> list[User]:
                seen["ours"] = _shape(selected_fields(info))
                seen["strawberry"] = _shape(info.selected_fields)
                return [User()]

        result = strawberry.Schema(query=Query).execute_sync(
            """
            query ($n: Int!) {
              users(first: $n) {
                name
                ... on User { posts(first: 2) { title } }
                ...More
              }
            }
            fragment More on User { latest: posts(first: 1) { title } }
            """,
            variable_values={"n": 3},
        )
        assert result.errors is None
        assert seen["ours"] == seen["strawberry"]

    def test_a_stand_in_without_the_query_gives_its_own(self):
        info = SimpleNamespace(field_nodes=[], selected_fields=["own"])
        assert selected_fields(info) == ["own"]

    def test_a_stand_in_without_either_selects_nothing(self):
        assert selected_fields(SimpleNamespace(field_nodes=[])) == []


class TestEdgesAnswered:
    def test_a_stand_in_for_info_is_passed_on(self):
        info = SimpleNamespace()
        assert _edges_answered(info) is info
