"""Unit tests for optimizer GraphQL selection walking."""

from graphql import parse

from strawberry_orm.optimizer.selections import iter_field_nodes


class TestIterFieldNodes:
    def test_yields_fields_from_inline_fragments(self):
        doc = parse("{ items { ... on ItemA { id name } ... on ItemB { id } } }")
        field_node = doc.definitions[0].selection_set.selections[0]
        names = [
            node.name.value for node in iter_field_nodes(field_node.selection_set, {})
        ]
        assert names == ["id", "name", "id"]

    def test_yields_fields_from_fragment_spread(self):
        doc = parse(
            """
            fragment ItemFields on Item { id title }
            { items { ...ItemFields } }
            """
        )
        fragments = {
            defn.name.value: defn
            for defn in doc.definitions
            if defn.kind == "fragment_definition"
        }
        operation = next(
            defn for defn in doc.definitions if defn.kind == "operation_definition"
        )
        field_node = operation.selection_set.selections[0]
        names = [
            node.name.value
            for node in iter_field_nodes(field_node.selection_set, fragments)
        ]
        assert names == ["id", "title"]

    def test_skips_unknown_fragment_spread(self):
        doc = parse("{ items { ...Missing } }")
        field_node = doc.definitions[0].selection_set.selections[0]
        assert list(iter_field_nodes(field_node.selection_set, {})) == []

    def test_none_selection_set(self):
        assert list(iter_field_nodes(None, {})) == []
