"""Each name a schema publishes stands for one type.

Strawberry keeps one type per GraphQL name. Two classes with the same
qualified name it takes for one type, so that a module imported twice still
builds, and it publishes whichever it converts first for both. The library
builds its types with ``type()``, naming each after a model, a node type, a
resolver or a name it is given, so two it builds differently under one name
share a qualified name too: a field typed with the one left out is served the
other, with its fields, its arguments and what it is cut to. A connection
over ``PublicPost`` serves ``StaffPost`` nodes; a ref list that takes no
``delete`` is published taking one.

So for every name the library builds - a connection over a node type, or over
one named lazily; a grouped connection with its page info, groups and group
items, over node types or over connection types of the app's own; a filter,
one cut for two node types, one built before and after the filter of a model
it relates to, a custom filter type, an order, a group-by with
its group key, an aggregates type, an input, a partial input, a ref, a
payload, and a node mutation input, named or not - two definitions that
differ either get names of their own, each with its own fields, or
``orm.schema()`` refuses them. Two that do not differ build, published once.

A backend provides ``orm``, built with a ``PayloadPolicy``, and ``models``
(``User``, ``Post``).
"""

import re
from collections.abc import Iterable
from typing import Annotated

import pytest
import strawberry
from graphql import get_named_type
from strawberry import relay

from strawberry_orm import auto, filter_field
from strawberry_orm.relay import ORMListConnection

REFUSED_MESSAGE = re.compile(r"takes an? (\w+) other than the one the schema publishes")

#: The node types the lazy references name: the last ones ``lazily_named`` built.
LazyPublicPost = None
LazyStaffPost = None
LAZY_PUBLIC = Annotated["LazyPublicPost", strawberry.lazy(__name__)]
LAZY_STAFF = Annotated["LazyStaffPost", strawberry.lazy(__name__)]


def node(orm, Post, name, *, shows_body=False, class_name="PostNode", **options):
    """A node type over *Post* published as *name*, showing its ``body`` or not."""
    annotations = {"id": relay.NodeID[int], "title": auto}
    if shows_body:
        annotations["body"] = auto
    cls = type(class_name, (relay.Node,), {"__annotations__": annotations})
    return orm.type(Post, name=name, **options)(cls)


def nodes(orm, Post, differ, **options):
    """``PublicPost``, and ``StaffPost`` showing more, or ``OtherPost`` the same."""
    public = node(orm, Post, "PublicPost", **options)
    if differ:
        return public, node(orm, Post, "StaffPost", shows_body=True, **options)
    return public, node(orm, Post, "OtherPost", **options)


def query(orm, **fields):
    """A ``Query`` with an ``orm`` field of each annotation."""
    namespace = {"__annotations__": {}}
    for name, annotation in fields.items():
        is_list = getattr(annotation, "__origin__", None) is list
        namespace["__annotations__"][name] = annotation
        namespace[name] = (orm.field if is_list else orm.connection).eager()
    return strawberry.type(type("Query", (), namespace))


def mutation(**arguments):
    """A ``Mutation`` with a field taking each input as ``data``."""
    namespace = {}
    for name, input_type in arguments.items():

        def resolve(data) -> int:
            return 1

        resolve.__annotations__["data"] = input_type
        namespace[name] = strawberry.mutation(resolver=resolve)
    return strawberry.type(type("Mutation", (), namespace))


def connection_over_node_classes_of_one_name(orm, models, differ):
    public = node(orm, models.Post, "PublicPost")
    other = node(orm, models.Post, "StaffPost", shows_body=True) if differ else public
    Query = query(orm, public=ORMListConnection[public], other=ORMListConnection[other])
    return {"query": Query, "types": [public, other]}


def lazily_named(orm, models, differ):
    global LazyPublicPost, LazyStaffPost
    LazyPublicPost = node(orm, models.Post, "PublicPost", class_name="LazyPublicPost")
    LazyStaffPost = node(
        orm, models.Post, "StaffPost", shows_body=True, class_name="LazyStaffPost"
    )
    # A field naming its node lazily resolves its own rows.
    fields = {}
    for name, lazy in (
        ("public", LAZY_PUBLIC),
        ("other", LAZY_STAFF if differ else LAZY_PUBLIC),
    ):

        def resolve(self, info: strawberry.Info) -> Iterable[lazy]:
            return []

        fields[name] = orm.connection(ORMListConnection[lazy])(resolve)
    return {"query": strawberry.type(type("Query", (), fields))}


def grouped_over_node_types(orm, models, differ):
    group = orm.group(models.Post)
    public = node(orm, models.Post, "PublicPost", group=group)
    other = (
        node(orm, models.Post, "StaffPost", shows_body=True, group=group)
        if differ
        else public
    )
    Query = query(orm, public=ORMListConnection[public], other=ORMListConnection[other])
    return {"query": Query}


def pages_of_their_own(public, staff):
    @strawberry.type
    class PublicPostPage(ORMListConnection[public]):
        pass

    @strawberry.type
    class StaffPostPage(ORMListConnection[staff]):
        pass

    return PublicPostPage, StaffPostPage


def grouped_over_connection_types_of_their_own(orm, models, differ):
    group = orm.group(models.Post)
    public, staff = nodes(orm, models.Post, True, group=group)
    public_page, staff_page = pages_of_their_own(public, staff)
    return {
        "query": query(
            orm, public=public_page, other=staff_page if differ else public_page
        )
    }


def filtered(orm, models, differ):
    # The broader one first: the other way round orm.filter refuses it itself.
    staff_filter = orm.filter(models.Post)
    public_filter = (
        orm.filter(models.Post, include=["title"])
        if differ
        else orm.filter(models.Post)
    )
    public = node(orm, models.Post, "PublicPost", filters=public_filter)
    other = node(orm, models.Post, "OtherPost", filters=staff_filter)
    return {"query": query(orm, public=list[public], other=list[other])}


def filter_cut_for_two_node_types(orm, models, differ):
    public = node(orm, models.Post, "PublicPost", filters=orm.filter(models.Post))
    other = node(
        orm,
        models.Post,
        "StaffPost" if differ else "OtherPost",
        shows_body=differ,
        filters=orm.filter(models.Post),
    )
    # The staff field first, so the filter cut for it is the one published.
    return {"query": query(orm, other=list[other], public=list[public])}


def filtered_around_a_relation(orm, models, differ):
    User, Post = models.User, models.Post
    before = orm.filter(User)
    if not differ:
        orm.filter(User)
    # Traverses to its author with the user filter built last before it.
    post = node(orm, Post, "PublicPost", filters=orm.filter(Post))
    author = type(
        "AuthorNode",
        (relay.Node,),
        {"__annotations__": {"id": relay.NodeID[int], "name": auto}},
    )
    # Built again after the post filter, it traverses to the author's posts.
    author = orm.type(User, filters=orm.filter(User) if differ else before)(author)
    return {"query": query(orm, authors=list[author], posts=list[post])}


def custom_filter_type(orm, models, differ):
    def search():
        @orm.filter_type(models.Post)
        class PostSearch:
            title: auto

            @filter_field
            def wordy(self, value: bool, prefix: str):
                return None

        return PostSearch

    public = node(orm, models.Post, "PublicPost", filters=search())
    other = node(
        orm,
        models.Post,
        "OtherPost",
        filters=orm.filter(models.Post, include=["title"]) if differ else search(),
    )
    return {"query": query(orm, public=list[public], other=list[other])}


def ordered(orm, models, differ):
    public = node(
        orm,
        models.Post,
        "PublicPost",
        order=orm.order(models.Post, include=["title"] if differ else None),
    )
    other = node(orm, models.Post, "OtherPost", order=orm.order(models.Post))
    return {"query": query(orm, public=list[public], other=list[other])}


def grouped_by(orm, models, differ):
    public, staff = (
        node(
            orm,
            models.Post,
            name,
            group=orm.group(models.Post, include=["title"] if narrow else None),
        )
        for name, narrow in (("PublicPost", differ), ("StaffPost", False))
    )
    public_page, staff_page = pages_of_their_own(public, staff)
    return {"query": query(orm, public=public_page, other=staff_page)}


def aggregated(orm, models, differ):
    @orm.aggregate_type(models.Post)
    class Few:
        id: auto

    @orm.aggregate_type(models.Post)
    class Many:
        id: auto
        word_count: auto

    group = orm.group(models.Post)
    public, staff = (
        node(orm, models.Post, name, group=group, aggregate=aggregate)
        for name, aggregate in (
            ("PublicPost", Few if differ else Many),
            ("StaffPost", Many),
        )
    )
    public_page, staff_page = pages_of_their_own(public, staff)
    return {"query": query(orm, public=public_page, other=staff_page)}


def given_input(orm, models, differ, build="input"):
    narrow = {"include": ["title"]} if differ else {}
    builder = getattr(orm, build)
    # The broader one first: the other way round the input refuses it itself.
    other = builder(models.Post)
    return {
        "query": query(orm, posts=list[node(orm, models.Post, "PublicPost")]),
        "mutation": mutation(public=builder(models.Post, **narrow), other=other),
    }


def partial_input(orm, models, differ):
    return given_input(orm, models, differ, build="partial")


def ref(orm, models, differ):
    return {
        "query": query(orm, posts=list[node(orm, models.Post, "PublicPost")]),
        "mutation": mutation(
            public=list[orm.ref(models.Post)],
            other=list[orm.ref(models.Post, delete=differ)],
        ),
    }


def payload(orm, models, differ):
    public, staff = nodes(orm, models.Post, True)
    other = staff if differ else public

    class Public:
        @orm.payload.mutation
        def create(self) -> public:
            return None

    class Other:
        @orm.payload.mutation
        def create(self) -> other:
            return None

    Mutation = strawberry.type(
        type(
            "Mutation",
            (),
            {"public": vars(Public)["create"], "other": vars(Other)["create"]},
        )
    )
    return {"query": query(orm, posts=list[public]), "mutation": Mutation}


def node_mutation_input(orm, models, differ, **name):
    models_of_other = [models.Post, models.User] if differ else [models.Post]
    return {
        "query": query(orm, posts=list[node(orm, models.Post, "PublicPost")]),
        "mutation": mutation(
            public=orm.mutations.create_node_input(models=[models.Post], **name),
            other=orm.mutations.create_node_input(models=models_of_other, **name),
        ),
    }


def named_node_mutation_input(orm, models, differ):
    return node_mutation_input(orm, models, differ, name="PostWrite")


BUILDERS = {
    "a connection over node classes of one name": (
        connection_over_node_classes_of_one_name
    ),
    "a connection over lazily named node types": lazily_named,
    "a grouped connection over node types": grouped_over_node_types,
    "a grouped connection over connection types of their own": (
        grouped_over_connection_types_of_their_own
    ),
    "a filter": filtered,
    "a filter cut for two node types": filter_cut_for_two_node_types,
    "a filter built before and after a related model's": filtered_around_a_relation,
    "a custom filter type": custom_filter_type,
    "an order": ordered,
    "a group-by": grouped_by,
    "an aggregates type": aggregated,
    "an input": given_input,
    "a partial input": partial_input,
    "a ref": ref,
    "a payload": payload,
    "a node mutation input": node_mutation_input,
    "a named node mutation input": named_node_mutation_input,
}

GROUPED = ("PostConnection", "PostGroup", "PostPageInfo", "PostGroupItemsConnection")
FILTER = ("PostFilter", "PostField", "PostFilterObject")

#: The names two differing definitions may be refused under.
REFUSED = {
    "a connection over node classes of one name": ("PostNodeConnection",),
    "a grouped connection over node types": GROUPED,
    "a filter": FILTER,
    "a filter cut for two node types": FILTER,
    "a filter built before and after a related model's": ("UserFilter",),
    "a custom filter type": FILTER,
    "an order": ("PostOrder", "PostOrderField", "PostOrderObject"),
    "a group-by": ("PostGroupBy", "PostGroupByField", "PostGroupKey"),
    "an aggregates type": (
        *(
            f"Post{part}Aggregates"
            for part in ("", "Sum", "Avg", "Median", "Min", "Max")
        ),
        "PostGroupKey",
    ),
    "an input": ("PostInput",),
    "a partial input": ("PostPartialInput",),
    "a ref": ("PostRef",),
    "a payload": ("CreatePayload",),
    "a named node mutation input": ("PostWrite",),
}

#: For the definitions told apart by name, each one's type, by the fields
#: leading to it, and what its fields hold.
DISTINCT = {
    "a connection over lazily named node types": {
        ("Query", "public"): ("LazyPublicPostConnection", {"edges.node": "PublicPost"}),
        ("Query", "other"): ("LazyStaffPostConnection", {"edges.node": "StaffPost"}),
    },
    "a grouped connection over connection types of their own": {
        ("Query", "public"): (
            "PublicPostPage",
            {
                "edges.node": "PublicPost",
                "pageInfo": "PublicPostPagePageInfo",
                "groups": "PublicPostPageGroup",
                "groups.items": "PublicPostPageGroupItemsConnection",
                "groups.items.edges.node": "PublicPost",
            },
        ),
        ("Query", "other"): (
            "StaffPostPage",
            {
                "edges.node": "StaffPost",
                "pageInfo": "StaffPostPagePageInfo",
                "groups": "StaffPostPageGroup",
                "groups.items": "StaffPostPageGroupItemsConnection",
                "groups.items.edges.node": "StaffPost",
            },
        ),
    },
    "a node mutation input": {
        ("Mutation", "public", "data"): (None, {"post": "CreatePostNodeInput"}),
        ("Mutation", "other", "data"): (
            None,
            {"post": "CreatePostNodeInput", "user": "CreateUserNodeInput"},
        ),
    },
}

#: The type two of the same definition are published as, once.
PUBLISHED = {
    "a connection over node classes of one name": "PostNodeConnection",
    "a connection over lazily named node types": "LazyPublicPostConnection",
    "a grouped connection over node types": "PostConnection",
    "a grouped connection over connection types of their own": "PublicPostPage",
    "a filter": "PostFilter",
    "a filter cut for two node types": "PostFilter",
    "a filter built before and after a related model's": "UserFilter",
    "a custom filter type": "PostFilter",
    "an order": "PostOrder",
    "a group-by": "PostGroupBy",
    "an aggregates type": "PostAggregates",
    "an input": "PostInput",
    "a partial input": "PostPartialInput",
    "a ref": "PostRef",
    "a payload": "CreatePayload",
    "a node mutation input": "CreatePostNodeInput",
    "a named node mutation input": "PostWrite",
}

assert REFUSED.keys() | DISTINCT.keys() == BUILDERS.keys() == PUBLISHED.keys()
assert not REFUSED.keys() & DISTINCT.keys()


def published_type(schema, path):
    """The type the field (or the field's argument) at *path* is published with."""
    type_name, field_name, *argument = path
    field = schema._schema.get_type(type_name).fields[field_name]
    held = field.args[argument[0]].type if argument else field.type
    return get_named_type(held)


def holds(schema, graphql_type, path):
    """The name of the type *path*, dotted fields, leads to from *graphql_type*."""
    for name in path.split("."):
        graphql_type = get_named_type(graphql_type.fields[name].type)
    return graphql_type.name


class AbstractTestGeneratedTypeNames:
    def build(self, orm, models, builder, differ):
        return orm.schema(**BUILDERS[builder](orm, models, differ))

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_differing_definitions_get_names_of_their_own_or_are_refused(
        self, orm, models, builder
    ):
        if builder in REFUSED:
            with pytest.raises(TypeError) as refused:
                self.build(orm, models, builder, differ=True)
            found = REFUSED_MESSAGE.search(str(refused.value))
            assert found, f"{builder}: refused otherwise: {refused.value}"
            assert found.group(1) in REFUSED[builder], (
                f"{builder}: refused naming {found.group(1)}"
            )
            assert "Strawberry keeps one type per name" in str(refused.value), (
                refused.value
            )
            return

        schema = self.build(orm, models, builder, differ=True)
        published = {}
        for path, (name, held) in DISTINCT[builder].items():
            graphql_type = published_type(schema, path)
            published[path] = graphql_type.name
            if name is not None:
                assert graphql_type.name == name, (
                    f"{builder}: {path} is published as {graphql_type.name}"
                )
            assert set(graphql_type.fields) >= {p.split(".")[0] for p in held}, (
                f"{builder}: {graphql_type.name} has {sorted(graphql_type.fields)}"
            )
            assert {p: holds(schema, graphql_type, p) for p in held} == held, (
                f"{builder}: {graphql_type.name} holds otherwise"
            )
            if graphql_type.name == "StaffPostPage":
                node_type = holds(schema, graphql_type, "edges.node")
                assert "body" in schema._schema.get_type(node_type).fields, node_type
        assert len(set(published.values())) == len(published), (
            f"{builder}: published as {published}"
        )

    @pytest.mark.parametrize("builder", BUILDERS)
    def test_the_same_definition_built_twice_is_published_once(
        self, orm, models, builder
    ):
        schema = self.build(orm, models, builder, differ=False)
        assert schema._schema.get_type(PUBLISHED[builder]) is not None, (
            f"{builder}: no {PUBLISHED[builder]} in {sorted(schema._schema.type_map)}"
        )
