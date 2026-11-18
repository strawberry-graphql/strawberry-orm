"""A field the library is asked to build carries everything it was declared with.

``orm.field.eager(...)``, ``orm.connection.eager()`` and a resolver-less
``strawberry.field(...)`` on a relation of an ``@orm.type`` all leave the
resolver to the library. Whatever else the declaration says - a scope, a
permission, ``filters=`` and ``order=``, a description, a deprecation, tags -
ends up on the field the library builds, or the declaration is refused while
the schema is built. None of it is left to Strawberry's default resolver,
which reads the attribute off the row: whatever the ORM happened to load,
unscoped.

Backends supply:

``make_orm(**options)``               a fresh ORM, ``lazy_resolution="off"``
                                      unless *options* say otherwise
``models``                            ``User``, ``Post``, ``Comment``
``narrow.only(query, model, f, v)``   *query* keeping the rows whose *f* is *v*
``narrow.exclude(query, model, f, v)`` *query* without the rows whose *f* is *v*
``held(model, *ids)``                 awaitable of those rows, loaded up front
``run(schema, query, context)``       awaitable result of running *query*

The seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures". Bob and Alice commented on "Hello
World", Charlie on "GraphQL Guide".
"""

from types import SimpleNamespace

import pytest
import strawberry
from strawberry import relay
from strawberry.permission import BasePermission, PermissionExtension

from strawberry_orm.relay import ORMListConnection
from strawberry_orm.relay.connection import NodeType
from strawberry_orm.tags import TAGS_KEY
from strawberry_orm.types import auto
from tests.abstract import declared_at_module_scope


class Allow(BasePermission):
    def has_permission(self, source, info, **kwargs):
        return True


class Deny(BasePermission):
    message = "Denied."

    def has_permission(self, source, info, **kwargs):
        return False


DESCRIBED = {"description": "Their rows.", "deprecation_reason": "Gone."}

# name: (whether the edge scope is declared, declaration(orm, scope, inputs))
DECLARATIONS = {
    "annotation": (False, lambda orm, scope, inputs: None),
    "scope": (True, lambda orm, scope, inputs: orm.field.eager(scope=scope)),
    "scope_permitted": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope, permission_classes=[Allow]
        ),
    ),
    "scope_permitted_positional": (
        True,
        lambda orm, scope, inputs: orm.field.eager(scope, permission_classes=[Allow]),
    ),
    "scope_permitted_unoptimized": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope, permission_classes=[Allow], disable_optimization=True
        ),
    ),
    "scope_described": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope, tags="kept", **DESCRIBED
        ),
    ),
    "scope_permitted_described": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope, permission_classes=[Allow], tags="kept", **DESCRIBED
        ),
    ),
    "permitted": (
        False,
        lambda orm, scope, inputs: orm.field.eager(permission_classes=[Allow]),
    ),
    "described": (
        False,
        lambda orm, scope, inputs: orm.field.eager(tags="kept", **DESCRIBED),
    ),
    "unoptimized": (
        False,
        lambda orm, scope, inputs: orm.field.eager(disable_optimization=True),
    ),
    "app_permitted": (
        False,
        lambda orm, scope, inputs: strawberry.field(permission_classes=[Allow]),
    ),
    "app_described": (
        False,
        lambda orm, scope, inputs: strawberry.field(
            metadata={TAGS_KEY: frozenset({"kept"})}, **DESCRIBED
        ),
    ),
    "app_extension": (
        False,
        lambda orm, scope, inputs: strawberry.field(
            extensions=[PermissionExtension(permissions=[Allow()])]
        ),
    ),
    "app_defaulted": (
        False,
        lambda orm, scope, inputs: strawberry.field(default=None),
    ),
}

DENIALS = {
    "scope_denied": lambda orm, scope, inputs: orm.field.eager(
        scope=scope, permission_classes=[Deny]
    ),
    "denied": lambda orm, scope, inputs: orm.field.eager(permission_classes=[Deny]),
    "app_denied": lambda orm, scope, inputs: strawberry.field(
        permission_classes=[Deny]
    ),
    "app_extension_denied": lambda orm, scope, inputs: strawberry.field(
        extensions=[PermissionExtension(permissions=[Deny()])]
    ),
}

# name: (whether the edge scope is declared, declaration(orm, scope, inputs))
ARGUMENTS = {
    "filters": (
        False,
        lambda orm, scope, inputs: orm.field.eager(filters=inputs.filters),
    ),
    "order": (False, lambda orm, scope, inputs: orm.field.eager(order=inputs.order)),
    "filters_order_permitted": (
        False,
        lambda orm, scope, inputs: orm.field.eager(
            filters=inputs.filters, order=inputs.order, permission_classes=[Allow]
        ),
    ),
    "scope_filters_order": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope, filters=inputs.filters, order=inputs.order
        ),
    ),
    "scope_filters_order_permitted": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope,
            filters=inputs.filters,
            order=inputs.order,
            permission_classes=[Allow],
        ),
    ),
    "scope_filters_order_unoptimized": (
        True,
        lambda orm, scope, inputs: orm.field.eager(
            scope=scope,
            filters=inputs.filters,
            order=inputs.order,
            disable_optimization=True,
        ),
    ),
}

# path: (ORM options, schema options, the root field read)
PATHS = {
    "planned": ({}, {}, "rows"),
    "unoptimized": ({}, {"optimizer": False}, "rows"),
    # The first read loads the rows' comments, which marks them as loaded for
    # the planner, so the second read meets rows it will not load again.
    "held_twice": ({}, {}, "held"),
    "held_twice_warned": ({"lazy_resolution": "warn"}, {}, "held"),
}


def _post_type(orm, models, annotations, **attributes):
    annotations = {"title": auto, **annotations}
    return _declare(orm, models.Post, "PostType", annotations, **attributes)


# Each declares a PostType the library cannot build as declared.
CANNOT_BUILD = {
    "filters_on_a_to_one": lambda orm, models, types: _post_type(
        orm,
        models,
        {"author": types.UserType | None},
        author=orm.field.eager(filters=orm.filter(models.User)),
    ),
    "order_on_a_scoped_to_one": lambda orm, models, types: _post_type(
        orm,
        models,
        {"author": types.UserType | None},
        author=orm.field.eager(
            scope=lambda query, info: query, order=orm.order(models.User)
        ),
    ),
    "filters_on_a_column": lambda orm, models, types: _post_type(
        orm, models, {}, title=orm.field.eager(filters=orm.filter(models.Post))
    ),
    "filters_on_another_relations_view": lambda orm, models, types: _post_type(
        orm,
        models,
        {"replies": list[types.CommentType]},
        replies=orm.field.eager(on="comments", filters=orm.filter(models.Comment)),
    ),
    "a_declaration_without_a_type": lambda orm, models, types: _post_type(
        orm, models, {}, comments=orm.field.eager(permission_classes=[Allow])
    ),
    "a_bare_declaration_without_a_type": lambda orm, models, types: _post_type(
        orm, models, {}, comments=orm.field.eager()
    ),
    "a_default_on_a_relation": lambda orm, models, types: _post_type(
        orm, models, {"author": types.UserType | None}, author=None
    ),
    "a_connection_without_a_type": lambda orm, models, types: _post_type(
        orm, models, {}, comments=orm.connection.eager()
    ),
    "a_connection_over_a_node_declared_later": lambda orm, models, types: (
        declared_at_module_scope.declare_a_connection_before_its_node(orm, models)
    ),
}

ALICE = {"name": "Alice", "email": "alice@example.com"}
BOB = {"name": "Bob", "email": "bob@example.com"}


def _declare(orm, model, name, annotations, **attributes):
    """``@orm.type(model)`` over class *name*, built as a class statement would."""
    namespace = {"__annotations__": annotations, **attributes}
    return orm.type(model)(type(name, (), namespace))


def _declared(name, declaration):
    return {} if declaration is None else {name: declaration}


def _held(info: strawberry.Info):
    return info.context["held"]


def _schema(orm, parent, **options):
    """``rows`` reads every *parent*; ``held`` returns the rows the test loaded."""
    namespace = {
        "__annotations__": {"rows": list[parent]},
        "rows": orm.field.eager(),
        "held": strawberry.field(resolver=_held, graphql_type=list[parent]),
    }
    return orm.schema(query=strawberry.type(type("Query", (), namespace)), **options)


def _posts_of_users(orm, models, narrow, declare):
    """``UserType.posts`` as *declare* says; ``PostType`` hides "Rust Adventures".

    The edge scope keeps published posts.
    """
    User, Post, Comment = models.User, models.Post, models.Comment

    @orm.type(Post)
    class PostType:
        title: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, Post, "title", "Rust Adventures")

    @orm.type(Comment)
    class CommentType:
        body: auto

    inputs = SimpleNamespace(filters=orm.filter(Post), order=orm.order(Post))
    published = lambda query, info: narrow.only(query, Post, "is_published", True)  # noqa: E731
    return _declare(
        orm,
        User,
        "UserType",
        {"name": auto, "comments": list[CommentType], "posts": list[PostType]},
        **_declared("posts", declare(orm, published, inputs)),
    )


def _authors_of_posts(orm, models, narrow, declare, *, nullable=True):
    """``PostType.author`` as *declare* says; ``UserType`` hides Charlie.

    The edge scope keeps Alice.
    """
    User, Post, Comment = models.User, models.Post, models.Comment

    @orm.type(User)
    class UserType:
        name: auto
        email: auto

        @classmethod
        def scope_rows(cls, query, info):
            return narrow.exclude(query, User, "name", "Charlie")

    @orm.type(Comment)
    class CommentType:
        body: auto

    inputs = SimpleNamespace(filters=orm.filter(User), order=orm.order(User))
    alices = lambda query, info: narrow.only(query, User, "name", "Alice")  # noqa: E731
    return _declare(
        orm,
        Post,
        "PostType",
        {
            "title": auto,
            "comments": list[CommentType],
            "author": UserType | None if nullable else UserType,
        },
        **_declared("author", declare(orm, alices, inputs)),
    )


SHAPES = {
    "to_many": lambda orm, models, narrow, declare: _posts_of_users(
        orm, models, narrow, declare
    ),
    "to_one": lambda orm, models, narrow, declare: _authors_of_posts(
        orm, models, narrow, declare
    ),
    "to_one_required": lambda orm, models, narrow, declare: _authors_of_posts(
        orm, models, narrow, declare, nullable=False
    ),
}


def _query(shape, root, arguments=""):
    """Read *shape*'s relation under *root*; ``held`` is read a second time."""
    if shape == "to_many":
        read = f"{root} {{ name posts{arguments} {{ title }} }}"
    else:
        read = f"{root} {{ title author{arguments} {{ name email }} }}"
    if root == "held":
        return f"{{ first: held {{ comments {{ body }} }} {read} }}"
    return f"{{ {read} }}"


def _expected(shape, edge):
    if shape == "to_many":
        return {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [] if edge else ["Draft Post"],
            "Charlie": [],
        }
    return {
        "Hello World": ALICE,
        "GraphQL Guide": ALICE,
        "Draft Post": None if edge else BOB,
        "Rust Adventures": None,
    }


def _read(result, shape, root, *, ordered=False):
    assert result.errors is None, result.errors
    if shape == "to_many":
        return {
            user["name"]: [post["title"] for post in user["posts"]]
            if ordered
            else sorted(post["title"] for post in user["posts"])
            for user in result.data[root]
        }
    return {post["title"]: post["author"] for post in result.data[root]}


def _outcome(result):
    return result.data, sorted({error.message for error in result.errors or ()})


def _field(cls, name):
    return next(
        f for f in cls.__strawberry_definition__.fields if f.python_name == name
    )


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::UserWarning")
class AbstractTestDeclaredFieldOptions:
    async def _run(self, make_orm, models, narrow, run, held, shape, declare, path):
        orm_options, schema_options, root = PATHS[path]
        orm = make_orm(**orm_options)
        parent = SHAPES[shape](orm, models, narrow, declare)
        schema = _schema(orm, parent, **schema_options)
        if shape == "to_many":
            rows = await held(models.User, 1, 2, 3)
        else:
            rows = await held(models.Post, 1, 2, 3, 4)
        return parent, root, schema, {"held": rows}

    @pytest.mark.parametrize("path", list(PATHS))
    @pytest.mark.parametrize("shape", list(SHAPES))
    @pytest.mark.parametrize("declaration", list(DECLARATIONS))
    async def test_a_relation_reads_what_its_declaration_allows(
        self, make_orm, models, narrow, run, held, declaration, shape, path
    ):
        edge, declare = DECLARATIONS[declaration]
        _, root, schema, context = await self._run(
            make_orm, models, narrow, run, held, shape, declare, path
        )
        result = await run(schema, _query(shape, root), context)
        if shape == "to_one_required":
            control = DECLARATIONS["scope" if edge else "annotation"][1]
            _, _, unguarded, _ = await self._run(
                make_orm, models, narrow, run, held, shape, control, path
            )
            expected = await run(unguarded, _query(shape, root), context)
            assert _outcome(result) == _outcome(expected)
            assert "charlie@test.org" not in str(result.data)
            if edge:
                assert "bob@example.com" not in str(result.data)
        else:
            assert _read(result, shape, root) == _expected(shape, edge)

    @pytest.mark.parametrize("path", list(PATHS))
    @pytest.mark.parametrize("shape", ["to_many", "to_one"])
    @pytest.mark.parametrize("declaration", list(DENIALS))
    async def test_a_denied_relation_returns_nothing(
        self, make_orm, models, narrow, run, held, declaration, shape, path
    ):
        _, root, schema, context = await self._run(
            make_orm, models, narrow, run, held, shape, DENIALS[declaration], path
        )
        result = await run(schema, _query(shape, root), context)
        assert {error.message for error in result.errors or ()} == {"Denied."}
        if shape == "to_many":
            assert "Hello World" not in str(result.data)
        else:
            assert "alice@example.com" not in str(result.data)

    @pytest.mark.parametrize("path", list(PATHS))
    @pytest.mark.parametrize("declaration", list(ARGUMENTS))
    async def test_declared_filters_and_order_are_arguments(
        self, make_orm, models, narrow, run, held, declaration, path
    ):
        edge, declare = ARGUMENTS[declaration]
        _, root, schema, context = await self._run(
            make_orm, models, narrow, run, held, "to_many", declare, path
        )
        bob = [] if edge else ["Draft Post"]
        if "filters" in declaration:
            where = '(filter: {field: {title: {neq: "Hello World"}}})'
            result = await run(schema, _query("to_many", root, where), context)
            assert _read(result, "to_many", root) == {
                "Alice": ["GraphQL Guide"],
                "Bob": bob,
                "Charlie": [],
            }
        if "order" in declaration:
            order = "(order: [{field: {title: DESC}}])"
            result = await run(schema, _query("to_many", root, order), context)
            assert _read(result, "to_many", root, ordered=True) == {
                "Alice": ["Hello World", "GraphQL Guide"],
                "Bob": bob,
                "Charlie": [],
            }

    @pytest.mark.parametrize(
        "declaration",
        ["described", "scope_described", "scope_permitted_described", "app_described"],
    )
    async def test_a_relation_keeps_its_description_deprecation_and_tags(
        self, make_orm, models, narrow, declaration
    ):
        orm = make_orm()
        UserType = _posts_of_users(orm, models, narrow, DECLARATIONS[declaration][1])
        posts = _field(UserType, "posts")
        assert posts.description == "Their rows."
        assert posts.deprecation_reason == "Gone."
        assert posts.metadata[TAGS_KEY] == {"kept"}

        sdl = str(_schema(orm, UserType))
        assert '"""Their rows."""' in sdl
        assert 'posts: [PostType!]! @deprecated(reason: "Gone.")' in sdl
        other = _schema(orm, UserType, tags="other")
        assert "posts" not in other._schema.get_type("UserType").fields

    @pytest.mark.parametrize("permitted", [False, True])
    async def test_a_column_keeps_its_description_and_deprecation(
        self, make_orm, models, permitted
    ):
        orm = make_orm()
        options = {"permission_classes": [Allow]} if permitted else {}

        @orm.type(models.User)
        class UserType:
            name: auto = orm.field.eager(**options, **DESCRIBED)

        name = _field(UserType, "name")
        assert (name.description, name.deprecation_reason) == ("Their rows.", "Gone.")

    @pytest.mark.parametrize("declared", list(CANNOT_BUILD))
    async def test_a_declaration_the_library_cannot_build_is_refused(
        self, make_orm, models, declared
    ):
        orm = make_orm()

        @orm.type(models.User)
        class UserType:
            name: auto

        @orm.type(models.Comment)
        class CommentType:
            body: auto

        types = SimpleNamespace(UserType=UserType, CommentType=CommentType)
        with pytest.raises(ValueError, match=r"(PostType|EarlyNode)\.\w+"):
            CANNOT_BUILD[declared](orm, models, types)

    @pytest.mark.parametrize(
        "declared",
        [
            "a_field_without_a_type",
            "a_connection_without_a_type",
            "a_list_of_what_is_not_a_model",
            "a_list_of_a_node_declared_later",
            "a_connection_over_a_node_declared_later",
        ],
    )
    async def test_a_root_field_the_library_cannot_build_is_refused(
        self, make_orm, models, declared
    ):
        orm = make_orm()

        @orm.type(models.User)
        class UserType:
            name: auto

        if declared == "a_field_without_a_type":
            Query = strawberry.type(
                type(
                    "Query",
                    (),
                    {
                        "__annotations__": {"users": list[UserType]},
                        "users": orm.field.eager(),
                        "spare": orm.field.eager(),
                    },
                )
            )
        elif declared == "a_connection_without_a_type":
            Query = strawberry.type(
                type(
                    "Query",
                    (),
                    {
                        "__annotations__": {"users": list[UserType]},
                        "users": orm.field.eager(),
                        "spare": orm.connection.eager(),
                    },
                )
            )
        elif declared == "a_list_of_what_is_not_a_model":

            @strawberry.type
            class Query:
                users: list[UserType] = orm.field.eager()
                names: list[str] = orm.field.eager()

        else:
            Query = declared_at_module_scope.declare_query_before_its_types(
                orm, models, "list" if "list" in declared else "connection"
            )

        with pytest.raises(ValueError, match=r"Query\.\w+"):
            orm.schema(query=Query)

    @pytest.mark.parametrize("page", ["subclass", "grouped_subclass", "generic"])
    async def test_a_connection_keeps_its_own_connection_type(
        self, make_orm, models, narrow, run, page
    ):
        orm = make_orm()
        User, Post = models.User, models.Post
        options = {"group": orm.group(Post)} if page == "grouped_subclass" else {}

        @orm.type(Post, **options)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return narrow.exclude(query, Post, "title", "Rust Adventures")

        if page == "generic":

            @strawberry.type
            class Page(ORMListConnection[NodeType]):
                note: str = "page"

            PostPage = Page[PostNode]
        else:

            @strawberry.type
            class PostPage(ORMListConnection[PostNode]):
                note: str = "page"

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: PostPage = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()
            page: PostPage = orm.connection.eager()

        schema = orm.schema(query=Query)
        result = await run(
            schema,
            """{ users { name posts(first: 1) { note edges { node { title } } } }
                 page(first: 3) { note edges { node { title } } } }""",
            {},
        )
        assert result.errors is None, result.errors
        assert {
            user["name"]: (
                user["posts"]["note"],
                [edge["node"]["title"] for edge in user["posts"]["edges"]],
            )
            for user in result.data["users"]
        } == {
            "Alice": ("page", ["Hello World"]),
            "Bob": ("page", ["Draft Post"]),
            "Charlie": ("page", []),
        }
        assert result.data["page"]["note"] == "page"
        assert [edge["node"]["title"] for edge in result.data["page"]["edges"]] == [
            "Hello World",
            "GraphQL Guide",
            "Draft Post",
        ]

    async def test_types_named_as_strings_are_built_where_they_are_declared(
        self, make_orm, models, narrow, run
    ):
        orm = make_orm()
        schema = orm.schema(
            query=declared_at_module_scope.declare_node_types(
                orm, models, narrow, Allow
            )
        )
        result = await run(
            schema,
            """{ users { name posts(order: [{field: {title: DESC}}]) { title }
                         comments(first: 1) { edges { node { body } } } }
                 posts(filter: {field: {title: {neq: "Hello World"}}}) { title }
                 page(first: 1) { edges { node { title } } } }""",
            {},
        )
        assert result.errors is None, result.errors
        assert {
            user["name"]: (
                [post["title"] for post in user["posts"]],
                [edge["node"]["body"] for edge in user["comments"]["edges"]],
            )
            for user in result.data["users"]
        } == {
            "Alice": (["Hello World", "GraphQL Guide"], ["Thanks!"]),
            "Bob": ([], ["Nice post!"]),
            "Charlie": ([], ["Great guide"]),
        }
        assert sorted(post["title"] for post in result.data["posts"]) == [
            "Draft Post",
            "GraphQL Guide",
        ]
        assert [edge["node"]["title"] for edge in result.data["page"]["edges"]] == [
            "Hello World"
        ]
