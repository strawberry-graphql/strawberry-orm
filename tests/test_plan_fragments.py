"""Which fragments the optimizer's planner collects for a row's type."""

from types import SimpleNamespace

from graphql import build_schema, parse

from strawberry_orm.optimizer.planning import Planner

SCHEMA = build_schema(
    """
    interface Named { name: String }
    type Person implements Named { name: String, age: Int }
    type Place implements Named { name: String, size: Int }
    type Query { named: [Named] }
    """
)

DOCUMENT = """
{
  named {
    ... on Named { name }
    ... on Person { age }
    ... on Place { size }
  }
}
"""


def _collected(type_name):
    operation = parse(DOCUMENT).definitions[0]
    (field,) = operation.selection_set.selections
    info = SimpleNamespace(
        schema=SCHEMA,
        fragments={},
        variable_values={},
        operation=operation,
        field_nodes=[field],
    )
    planner = Planner(SimpleNamespace(), None, info)
    return sorted(planner._collect((field.selection_set,), type_name))


class TestFragmentConditions:
    def test_a_type_takes_its_own_fragments_and_its_interfaces(self):
        assert _collected("Person") == ["age", "name"]
        assert _collected("Place") == ["name", "size"]

    def test_a_type_another_schema_has_takes_every_fragment(self):
        # One model may be registered as types this schema does not have;
        # planning what they would select only loads more.
        assert _collected("Elsewhere") == ["age", "name", "size"]
