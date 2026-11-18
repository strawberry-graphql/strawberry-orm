"""A tagged argument or input field is out of a schema however it was made.

Each case declares something tagged ``internal`` in a different way. In the
``public`` schema it must be missing from the SDL and refused when sent; in the
``internal`` schema and the untagged one it must be there and take effect. The
untagged arguments next to it must stay everywhere.
"""

import copy
import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
import strawberry
from graphql import GraphQLInputObjectType, get_named_type, parse, validate
from strawberry import relay
from strawberry.annotation import StrawberryAnnotation
from strawberry.extensions import FieldExtension
from strawberry.field_extensions import InputMutationExtension
from strawberry.types.arguments import StrawberryArgument
from strawberry.types.fields.resolver import StrawberryResolver

from strawberry_orm import PayloadPolicy, StrawberryORM, auto, tags
from strawberry_orm.relay import ORMListConnection
from tests.backends.sqlalchemy.models import Post, User

INTERNAL = Annotated[bool, strawberry.argument(metadata=tags("internal"))]


@strawberry.type
class TgProblem:
    message: str


def _orm() -> StrawberryORM:
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=PayloadPolicy(errors=TgProblem, on_error=lambda error: None),
    )


orm = _orm()


class TgAddsArgument(FieldExtension):
    """Adds a tagged argument of its own, which shouts the name."""

    def apply(self, field):
        field.arguments = [
            *field.arguments,
            StrawberryArgument(
                python_name="added_secret",
                graphql_name=None,
                type_annotation=StrawberryAnnotation(bool),
                default=False,
                metadata=tags("internal"),
            ),
        ]

    def map_arguments(self, kwargs):
        if kwargs.pop("added_secret", False):
            kwargs["name"] = kwargs["name"].upper()
        return kwargs


class TgRebuildsArguments(FieldExtension):
    """Rebuilds the arguments from the resolver, leaving their metadata behind."""

    def apply(self, field):
        field.arguments = [
            StrawberryArgument(
                python_name=argument.python_name,
                graphql_name=argument.graphql_name,
                type_annotation=argument.type_annotation,
                default=argument.default,
            )
            for argument in StrawberryResolver(
                field.base_resolver.wrapped_func
            ).arguments
        ]


class TgFreshInput(InputMutationExtension):
    """Strawberry's ``input`` built from a fresh read of the resolver."""

    def apply(self, field):
        fresh = copy.copy(field)
        fresh.base_resolver = StrawberryResolver(field.base_resolver.wrapped_func)
        super().apply(fresh)
        field.arguments = fresh.arguments


class TgReusedInput(InputMutationExtension):
    """Strawberry's ``input``, built once and handed to every schema after."""

    def apply(self, field):
        if "built" not in vars(self):
            super().apply(field)
            self.built = field.arguments
        field.arguments = self.built


class TgAliasesArgument(FieldExtension):
    """Takes an untagged ``alias`` and hands it to the resolver under another name."""

    def apply(self, field):
        field.arguments = [
            *field.arguments,
            StrawberryArgument(
                python_name="alias",
                graphql_name=None,
                type_annotation=StrawberryAnnotation(bool),
                default=False,
            ),
        ]

    def map_arguments(self, kwargs):
        if kwargs.pop("alias", False):
            kwargs["aliased_secret"] = True
        return kwargs


@strawberry.input
class TgSignUp:
    name: str
    input_secret: bool = strawberry.field(default=False, metadata=tags("internal"))


def _world() -> tuple[StrawberryORM, dict[str, dict[str, type]]]:
    """The roots of every case, made afresh, and the ORM they were made with."""
    orm = _orm()

    @orm.type(Post)
    class TgPostType:
        id: auto
        title: auto = orm.field.eager(tags="internal")
        body: auto

    @orm.filter_type(User)
    class TgUserFilter:
        id: auto
        name: auto
        email: auto = strawberry.field(metadata=tags("internal"))

    @orm.order_type(User)
    class TgUserOrder:
        id: auto
        name: auto
        email: auto = strawberry.field(metadata=tags("internal"))

    @orm.group_type(User)
    class TgUserGroup:
        name: auto
        email: auto = strawberry.field(metadata=tags("internal"))

    @orm.type(User, filters=TgUserFilter, order=TgUserOrder, group=TgUserGroup)
    class TgUserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        email: auto

    TgPostFilter = orm.filter(Post)
    TgPostOrder = orm.order(Post)

    @strawberry.type
    class Query:
        @strawberry.field
        def argument(self, name: str, argument_secret: INTERNAL = False) -> str:
            return f"{name} {argument_secret}"

        @strawberry.field(extensions=[TgRebuildsArguments()])
        def rebuilt(self, name: str, rebuilt_secret: INTERNAL = False) -> str:
            return f"{name} {rebuilt_secret}"

        @strawberry.field(extensions=[TgAliasesArgument()])
        def aliased(self, name: str, aliased_secret: INTERNAL = False) -> str:
            return f"{name} {aliased_secret}"

        @strawberry.field
        def signed_up(self, input: TgSignUp) -> str:
            return f"{input.name} {input.input_secret}"

        @orm.connection(ORMListConnection[TgUserNode])
        def page(
            self, info: strawberry.Info, page_secret: INTERNAL = False
        ) -> Iterable[User]:
            raise NotImplementedError

        @orm.field.lazy(filters=TgPostFilter, order=TgPostOrder)
        def posts(
            self,
            info: strawberry.Info,
            filter: Annotated[
                TgPostFilter | None, strawberry.argument(metadata=tags("internal"))
            ] = None,
        ) -> list[TgPostType]:
            raise NotImplementedError

        all_posts: list[TgPostType] = orm.field.eager(
            filters=TgPostFilter, order=TgPostOrder
        )
        users: list[TgUserNode] = orm.field.eager()

    @strawberry.type
    class Mutation:
        @orm.payload.mutation
        def payload(self, name: str, payload_secret: INTERNAL = False) -> str:
            return f"{name} {payload_secret}"

        @orm.payload.mutation(input_mutation=True)
        def payload_input(
            self, name: str, payload_input_secret: INTERNAL = False
        ) -> str:
            return f"{name} {payload_input_secret}"

        @strawberry.mutation(extensions=[InputMutationExtension()])
        def input_extension(
            self, name: str, input_extension_secret: INTERNAL = False
        ) -> str:
            return f"{name} {input_extension_secret}"

        @strawberry.mutation(extensions=[TgFreshInput()])
        def fresh_input(self, name: str, fresh_input_secret: INTERNAL = False) -> str:
            return f"{name} {fresh_input_secret}"

        @strawberry.mutation(extensions=[TgReusedInput()])
        def reused_input(self, name: str, reused_input_secret: INTERNAL = False) -> str:
            return f"{name} {reused_input_secret}"

    @strawberry.type
    class AddedQuery:
        @strawberry.field(extensions=[TgAddsArgument()])
        def added(self, name: str) -> str:
            return name

    return orm, {
        "main": {"query": Query, "mutation": Mutation},
        "added": {"query": AddedQuery},
    }


SELECTIONS = {"public": {"tags": "public"}, "internal": {"tags": "internal"}}
SELECTIONS["untagged"] = {}


#: The schemas built from the same roots before the one tested: each schema
#: converting a field applies the field's extensions to the field itself.
BEFORE = {"first": (), **{f"after {name}": (name,) for name in SELECTIONS}}


@functools.cache
def _schema(roots: str, selection: str, before: str):
    made_with, every = _world()
    for earlier in BEFORE[before]:
        made_with.schema(**every[roots], **SELECTIONS[earlier])
    return made_with.schema(**every[roots], **SELECTIONS[selection])


def _args(root: str, field: str) -> Callable[[Any], set[str]]:
    def names(schema):
        graphql_type = getattr(schema._schema, f"{root}_type")
        return set(graphql_type.fields[field].args)

    return names


def _input_fields(root: str, field: str) -> Callable[[Any], set[str]]:
    def names(schema):
        graphql_type = getattr(schema._schema, f"{root}_type")
        found = set()
        for argument in graphql_type.fields[field].args.values():
            named = get_named_type(argument.type)
            if isinstance(named, GraphQLInputObjectType):
                found |= set(named.fields)
        return found

    return names


def _type_fields(name: str) -> Callable[[Any], set[str]]:
    return lambda schema: set(schema._schema.type_map[name].fields)


@dataclass(frozen=True)
class Case:
    secret: str
    names: Callable[[Any], set[str]]
    sent: str
    bare: str
    #: What the field returns for ``sent``, where the secret is kept and where not.
    kept: Any = None
    hidden: Any = None
    roots: str = "main"


CASES = {
    "argument": Case(
        "argumentSecret",
        _args("query", "argument"),
        '{ argument(name: "a", argumentSecret: true) }',
        '{ argument(name: "a") }',
        {"argument": "a True"},
    ),
    "payload argument": Case(
        "payloadSecret",
        _args("mutation", "payload"),
        'mutation { payload(name: "a", payloadSecret: true) { data } }',
        'mutation { payload(name: "a") { data } }',
        {"payload": {"data": "a True"}},
    ),
    "payload input_mutation": Case(
        "payloadInputSecret",
        _input_fields("mutation", "payloadInput"),
        'mutation { payloadInput(input: {name: "a", payloadInputSecret: true})'
        " { data } }",
        'mutation { payloadInput(input: {name: "a"}) { data } }',
        {"payloadInput": {"data": "a True"}},
    ),
    "InputMutationExtension": Case(
        "inputExtensionSecret",
        _input_fields("mutation", "inputExtension"),
        'mutation { inputExtension(input: {name: "a", inputExtensionSecret: true}) }',
        'mutation { inputExtension(input: {name: "a"}) }',
        {"inputExtension": "a True"},
    ),
    "extension's own input": Case(
        "freshInputSecret",
        _input_fields("mutation", "freshInput"),
        'mutation { freshInput(input: {name: "a", freshInputSecret: true}) }',
        'mutation { freshInput(input: {name: "a"}) }',
        {"freshInput": "a True"},
    ),
    "extension reuses its input": Case(
        "reusedInputSecret",
        _input_fields("mutation", "reusedInput"),
        'mutation { reusedInput(input: {name: "a", reusedInputSecret: true}) }',
        'mutation { reusedInput(input: {name: "a"}) }',
        {"reusedInput": "a True"},
    ),
    "extension adds it": Case(
        "addedSecret",
        _args("query", "added"),
        '{ added(name: "a", addedSecret: true) }',
        '{ added(name: "a") }',
        {"added": "A"},
        roots="added",
    ),
    "extension rebuilds it": Case(
        "rebuiltSecret",
        _args("query", "rebuilt"),
        '{ rebuilt(name: "a", rebuiltSecret: true) }',
        '{ rebuilt(name: "a") }',
        {"rebuilt": "a True"},
    ),
    "input field": Case(
        "inputSecret",
        _input_fields("query", "signedUp"),
        '{ signedUp(input: {name: "a", inputSecret: true}) }',
        '{ signedUp(input: {name: "a"}) }',
        {"signedUp": "a True"},
    ),
    "connection argument": Case(
        "pageSecret",
        _args("query", "page"),
        "{ page(first: 1, pageSecret: true) { edges { node { name } } } }",
        "{ page(first: 1) { edges { node { name } } } }",
    ),
    "generated filter argument": Case(
        "filter",
        _args("query", "posts"),
        '{ posts(filter: {field: {body: {exact: "x"}}}) { id } }',
        "{ posts(order: [{field: {id: ASC}}]) { id } }",
    ),
    "generated filter field": Case(
        "title",
        _type_fields("PostField"),
        '{ allPosts(filter: {field: {title: {exact: "x"}}}) { id } }',
        '{ allPosts(filter: {field: {body: {exact: "x"}}}) { id } }',
    ),
    "generated order field": Case(
        "title",
        _type_fields("PostOrderField"),
        "{ allPosts(order: [{field: {title: ASC}}]) { id } }",
        "{ allPosts(order: [{field: {body: ASC}}]) { id } }",
    ),
    "filter_type field": Case(
        "email",
        _type_fields("UserField"),
        '{ users(filter: {field: {email: {exact: "x"}}}) { name } }',
        '{ users(filter: {field: {name: {exact: "x"}}}) { name } }',
    ),
    "order_type field": Case(
        "email",
        _type_fields("UserOrderField"),
        "{ users(order: [{field: {email: ASC}}]) { name } }",
        "{ users(order: [{field: {name: ASC}}]) { name } }",
    ),
    "group_type field": Case(
        "email",
        _type_fields("UserGroupByField"),
        "{ users(groupBy: [{field: {email: true}}]) { name } }",
        "{ users(groupBy: [{field: {name: true}}]) { name } }",
    ),
    "extension renames onto it": Case(
        "alias",
        _args("query", "aliased"),
        '{ aliased(name: "a", alias: true) }',
        '{ aliased(name: "a") }',
        {"aliased": "a True"},
        {"aliased": "a False"},
    ),
}


class TgAddsRequired(FieldExtension):
    def __init__(self, annotation):
        self._annotation = annotation

    def apply(self, field):
        field.arguments = [
            *field.arguments,
            StrawberryArgument(
                python_name="required_secret",
                graphql_name=None,
                type_annotation=StrawberryAnnotation(self._annotation),
                metadata=tags("internal"),
            ),
        ]

    def map_arguments(self, kwargs):
        kwargs.pop("required_secret", None)
        return kwargs


@strawberry.input
class TgNamed:
    name: str


def test_an_argument_an_extension_adds_without_a_default_cannot_be_hidden():
    @strawberry.type
    class Query:
        @strawberry.field(extensions=[TgAddsRequired(bool)])
        def greet(self) -> str:
            return "hi"

    with pytest.raises(
        TypeError, match=r"Query.greet\(required_secret:\) is tagged internal"
    ):
        orm.schema(query=Query, tags="public")


def test_an_optional_argument_an_extension_adds_is_hidden():
    @strawberry.type
    class Query:
        @strawberry.field(extensions=[TgAddsRequired(bool | None)])
        def greet(self) -> str:
            return "hi"

    schema = orm.schema(query=Query, tags="public")
    assert set(schema._schema.query_type.fields["greet"].args) == set()


def test_an_input_the_resolver_declares_keeps_a_field_named_like_a_hidden_one():
    @strawberry.type
    class Query:
        @strawberry.field(extensions=[TgRebuildsArguments()])
        def take(self, input: TgNamed, name: INTERNAL = False) -> str:
            return f"{input.name} {name}"

    public = orm.schema(query=Query, tags="public")
    assert set(public._schema.query_type.fields["take"].args) == {"input"}
    result = public.execute_sync('{ take(input: {name: "a"}) }')
    assert result.data == {"take": "a False"}


def _errors(schema, document: str) -> list[str]:
    return [error.message for error in validate(schema._schema, parse(document))]


@pytest.mark.parametrize("before", list(BEFORE))
@pytest.mark.parametrize("selection", list(SELECTIONS))
@pytest.mark.parametrize("case", list(CASES))
def test_a_tagged_item_is_only_in_the_schemas_selecting_it(case, selection, before):
    spec = CASES[case]
    schema = _schema(spec.roots, selection, before)
    hidden = selection == "public"
    if spec.hidden is None:
        assert (spec.secret in spec.names(schema)) is not hidden, sorted(
            spec.names(schema)
        )
        assert bool(_errors(schema, spec.sent)) is hidden, _errors(schema, spec.sent)
    assert _errors(schema, spec.bare) == []


@pytest.mark.parametrize("before", list(BEFORE))
@pytest.mark.parametrize("selection", list(SELECTIONS))
@pytest.mark.parametrize(
    "case", [name for name, spec in CASES.items() if spec.kept is not None]
)
def test_a_tagged_item_takes_effect_only_where_selected(case, selection, before):
    spec = CASES[case]
    schema = _schema(spec.roots, selection, before)
    result = schema.execute_sync(spec.sent)
    if selection != "public":
        assert result.errors is None, result.errors
        assert result.data == spec.kept
    elif spec.hidden is not None:
        assert result.errors is None, result.errors
        assert result.data == spec.hidden
    else:
        assert result.errors, result.data
