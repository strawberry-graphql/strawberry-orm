"""How a schema tells two types of one name apart, field by field.

Classes built with ``type()`` in one module share a qualified name, so
Strawberry publishes the first of them for both.
"""

import pytest
import strawberry

from strawberry_orm.type_names import refuse_merged_types


def twin(annotations, namespace=None, *, input=False, name="Twin"):
    cls = type(name, (), {"__annotations__": annotations, **(namespace or {})})
    return (strawberry.input if input else strawberry.type)(cls)


def schema_of(first, second):
    @strawberry.type
    class Query:
        one: first
        other: second

    return strawberry.Schema(query=Query)


def taking(first, second):
    def one(self, settings: first) -> int:
        return 1

    def other(self, settings: second) -> int:
        return 1

    @strawberry.type
    class Query:
        one_field: int = strawberry.field(resolver=one)
        other_field: int = strawberry.field(resolver=other)

    return strawberry.Schema(query=Query)


def finding(annotation, default):
    def find(self, value: annotation = default) -> int:
        return 1

    return twin({}, {"find": strawberry.field(resolver=find)})


@pytest.mark.parametrize(
    ("first", "second", "difference"),
    [
        (
            twin({"value": int}),
            twin({"value": list[int]}),
            "its value is a list or nullable on only one of them",
        ),
        (twin({"value": int}), twin({"value": str}), "its value holds another type"),
        (
            finding(int, 1),
            twin({}, {"find": strawberry.field(resolver=lambda: 1, graphql_type=int)}),
            "its find takes other arguments",
        ),
        (finding(int, 1), finding(str, "1"), "its find takes another value"),
        (finding(int, 1), finding(int, 2), "its find takes another value"),
    ],
)
def test_a_type_differing_from_the_one_published_is_refused(first, second, difference):
    with pytest.raises(TypeError) as refused:
        refuse_merged_types(schema_of(first, second))
    assert (
        f"Query.other takes a Twin other than the one the schema publishes: {difference}."
        in str(refused.value)
    )


def test_an_input_defaulting_to_another_value_is_refused():
    first = twin({"limit": int}, {"limit": 10}, input=True)
    second = twin({"limit": int}, {"limit": 20}, input=True)

    with pytest.raises(TypeError) as refused:
        refuse_merged_types(taking(first, second))

    assert "Query.otherField(settings:) takes a Twin" in str(refused.value)
    assert "its limit defaults to another value." in str(refused.value)


def test_the_same_type_twice_is_served_in_its_place():
    first, second = twin({"value": int}), twin({"value": int})

    assert refuse_merged_types(schema_of(first, second)) == {second}
