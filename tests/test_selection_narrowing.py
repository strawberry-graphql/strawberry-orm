"""Re-rooting a selection set with ``narrow_info``.

``orm.optimize(data, info, at=...)`` leans on this to find the selections that
describe the rows, which are often not the ones on the resolved field.
"""

from graphql import parse
from graphql.language.ast import FieldNode

from strawberry_orm.optimizer.selections import field_nodes_from_info, narrow_info


class _RawInfo:
    def __init__(self, field_nodes, fragments=None):
        self.field_nodes = field_nodes
        self.fragments = fragments or {}


class _Info:
    def __init__(self, raw):
        self._raw_info = raw
        self.context = {}


def _info_for(query: str) -> _Info:
    """Build an ``info`` whose field nodes are the operation's selections."""
    document = parse(query)
    operation = document.definitions[0]
    nodes = [
        node
        for node in operation.selection_set.selections
        if isinstance(node, FieldNode)
    ]
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if hasattr(definition, "type_condition")
    }
    return _Info(_RawInfo(nodes, fragments))


class TestNarrowInfo:
    def test_re_roots_onto_a_named_child(self):
        info = _info_for("{ users { data { name posts { title } } } }")
        narrowed = narrow_info(info, "data")
        selected = {
            node.name.value
            for node in field_nodes_from_info(narrowed)[0].selection_set.selections
        }
        assert selected == {"name", "posts"}

    def test_follows_a_multi_step_path(self):
        info = _info_for("{ q { payload { data { title } } } }")
        narrowed = narrow_info(info, ["payload", "data"])
        nodes = field_nodes_from_info(narrowed)
        assert [node.name.value for node in nodes] == ["data"]

    def test_matches_a_camel_case_selection(self):
        info = _info_for("{ q { payloadData { title } } }")
        narrowed = narrow_info(info, "payload_data")
        assert [node.name.value for node in field_nodes_from_info(narrowed)] == [
            "payloadData"
        ]

    def test_looks_through_inline_fragments_and_spreads(self):
        info = _info_for(
            """
            { q { ... on Payload { data { title } } ...Rest } }
            fragment Rest on Payload { other { id } }
            """
        )
        narrowed = narrow_info(info, "data")
        assert [node.name.value for node in field_nodes_from_info(narrowed)] == ["data"]

    def test_a_path_that_is_not_selected_yields_nothing(self):
        """Nothing to load is not an error - the rows still come back."""
        info = _info_for("{ q { errors } }")
        assert field_nodes_from_info(narrow_info(info, "data")) == []

    def test_unlisted_attributes_read_through_to_the_original(self):
        info = _info_for("{ q { data { title } } }")
        assert narrow_info(info, "data").context is info.context


def test_narrowing_under_a_leaf_selection_yields_nothing():
    """A field with no sub-selection has no children to re-root onto."""
    info = _info_for("{ q }")
    assert field_nodes_from_info(narrow_info(info, "data")) == []
