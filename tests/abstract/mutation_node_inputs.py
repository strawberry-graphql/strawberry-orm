"""The generated node mutation inputs.

``create_node_input()`` / ``update_node_input()`` build a ``@oneOf`` input
carrying one branch per registered relay ``Node`` type, with recursive nested
ref lists. The library does not ship a resolver that executes them - the write
is yours to write, which is what keeps row scoping in your hands.

So what is verified here is what the library still owns: the shape of the
input, and the projection that narrows which relations are writable. Each case
posts an input at a hand-written resolver that reports which root branch was
selected, so acceptance is decided by the schema.
"""

from typing import Any

import pytest
import strawberry
from strawberry import relay
from strawberry.types.base import StrawberryOptional

from strawberry_orm.types import auto

CREATE_WITH_NESTED_RELATION = """
    mutation {
        inspectCreateNodeInput(input: {
            post: {
                title: "Typed only"
                body: "No built-in resolver"
                author: { create: { name: "Dana", email: "dana@example.com" } }
            }
        })
    }
"""

UPDATE_WITH_NESTED_RELATION = """
    mutation {
        inspectUpdateNodeInput(input: {
            comment: {
                id: "1"
                body: "Custom resolver update"
                author: { update: { id: "2", name: "Bobby" } }
            }
        })
    }
"""

# post.author and post.comments.author are declared by the projection.
PROJECTED_DECLARED_BRANCH = """
    mutation {
        inspectProjectedCreateNodeInput(input: {
            post: {
                title: "Projected Post"
                body: "Projected Body"
                author: {
                    create: {
                        name: "Projected Dana"
                        email: "projected-dana@example.com"
                    }
                    onReplace: DELETE
                }
                comments: [{
                    create: {
                        body: "Projected Comment"
                        author: {
                            create: {
                                name: "Projected Eve"
                                email: "projected-eve@example.com"
                            }
                            onReplace: DELETE
                        }
                    }
                }]
            }
        })
    }
"""

# The projection runs out before this nesting does, so the deepest `posts`
# has no field to land on.
PROJECTED_TOO_DEEP = """
    mutation {
        inspectProjectedCreateNodeInput(input: {
            post: {
                title: "Blocked Post"
                body: "Blocked Body"
                author: {
                    create: {
                        name: "Dana"
                        email: "dana@example.com"
                        posts: [{
                            create: {
                                title: "Too Deep"
                                body: "Still blocked"
                                author: {
                                    create: {
                                        name: "Nope"
                                        email: "nope@example.com"
                                        posts: [{
                                            create: {
                                                title: "Blocked Again"
                                                body: "Now really too deep"
                                            }
                                        }]
                                    }
                                }
                            }
                        }]
                    }
                }
            }
        })
    }
"""

# post.comments declares only `author`, so the comment's own `post` relation
# is absent rather than defaulting to a shallow arm.
PROJECTED_UNDECLARED_RELATION = """
    mutation {
        inspectProjectedCreateNodeInput(input: {
            post: {
                title: "Projected Post"
                body: "Projected Body"
                comments: [{
                    create: {
                        body: "Projected Comment"
                        post: { create: { title: "Sneaked", body: "In" } }
                    }
                }]
            }
        })
    }
"""

PROJECTED_UPDATE = """
    mutation {
        inspectProjectedUpdateNodeInput(input: {
            comment: {
                id: "1"
                body: "Projected update"
                author: { update: { id: "2", name: "Bobby" } }
            }
        })
    }
"""


UPSERT_USER_PROJECT = {
    "_meta": {"upsert": {"where": ["email"]}},
    "posts": {"_meta": {"create": True, "upsert": {"where": True}}},
}

UPSERT_USER_WITH_POSTS = """
    mutation {
        echoUpsertUser(input: {
            where: { email: "ada@example.com" }
            create: {
                name: "Ada"
                email: "ada@example.com"
                posts: [{ upsert: { where: { title: "First" }, create: { title: "First", body: "Body" } } }]
            }
        })
    }
"""


def _optional_fields(input_type: type) -> dict[str, bool]:
    return {
        field.python_name: isinstance(field.type, StrawberryOptional)
        for field in input_type.__strawberry_definition__.fields
    }


class UpsertNodeInputCases:
    def test_a_listed_where_requires_its_fields(self, orm, User, seed):
        upsert_input = orm.mutations.upsert_node_input(
            model=User, project=UPSERT_USER_PROJECT, name="UpsertUserListedWhere"
        )
        where_input = upsert_input.__strawberry_definition__.fields[0].type
        assert _optional_fields(where_input) == {"email": False}

    def test_a_true_where_offers_every_scalar_as_optional(self, orm, User, Post, seed):
        orm.mutations.upsert_node_input(
            model=User, project=UPSERT_USER_PROJECT, name="UpsertUserTrueWhere"
        )
        post_upsert = orm.mutations._upsert_input(
            Post,
            orm.mutations._normalize_model_project(Post, UPSERT_USER_PROJECT["posts"]),
        )
        where_input = post_upsert.__strawberry_definition__.fields[0].type
        optional = _optional_fields(where_input)
        assert {"id", "title", "body"} <= set(optional)
        assert "author_id" not in optional
        assert all(optional.values())

    def test_the_upsert_root_is_named_and_carries_its_model(self, orm, User, seed):
        upsert_input = orm.mutations.upsert_node_input(
            model=User, project=UPSERT_USER_PROJECT, name="UpsertUserNamed"
        )
        assert upsert_input.__strawberry_definition__.name == "UpsertUserNamed"
        assert upsert_input.__orm_model__ is User
        fields = {
            field.python_name: field.type
            for field in upsert_input.__strawberry_definition__.fields
        }
        assert set(fields) == {"where", "create", "update"}
        assert fields["create"].of_type.__orm_model__ is User

    def test_an_upsert_root_without_a_where_is_rejected(self, orm, User, seed):
        with pytest.raises(ValueError, match="requires _meta.upsert.where"):
            orm.mutations.upsert_node_input(
                model=User, project={"posts": {}}, name="UpsertUserNoWhere"
            )

    def test_an_optional_where_leaves_unsent_fields_unset(self, orm, User, Post, seed):
        upsert_input = orm.mutations.upsert_node_input(
            model=User, project=UPSERT_USER_PROJECT, name="UpsertUserEcho"
        )

        @strawberry.type
        class Query:
            ok: bool = True

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            def echo_upsert_user(self, input: upsert_input) -> str:  # type: ignore[valid-type]
                where = input.create.posts[0].upsert.where
                sent = sorted(
                    name
                    for name in type(where).__dataclass_fields__
                    if getattr(where, name) is not strawberry.UNSET
                )
                return f"{type(where).__orm_model__.__name__}:{','.join(sent)}"

        result = strawberry.Schema(query=Query, mutation=Mutation).execute_sync(
            UPSERT_USER_WITH_POSTS
        )
        assert result.errors is None
        assert result.data == {"echoUpsertUser": f"{Post.__name__}:title"}

    def test_a_named_project_names_its_types_and_recurses(self, orm, User, seed):
        upsert_input = orm.mutations.upsert_node_input(
            model=User, project=_named_user_project(), name="FurnishUserInput"
        )
        fields = _field_types(upsert_input)
        assert fields["where"].__strawberry_definition__.name == "FurnishUserWhere"
        user_create = fields["create"]
        assert user_create.__strawberry_definition__.name == "FurnishUserCreate"

        posts_item = _field_types(user_create)["posts"]
        assert posts_item.__strawberry_definition__.name == "FurnishUserPostsItem"
        post_create = _field_types(posts_item)["create"]
        assert post_create.__strawberry_definition__.name == "FurnishPostCreate"

        author_link = _field_types(post_create)["author"]
        assert author_link.__strawberry_definition__.name == "FurnishPostAuthorLink"
        assert _field_types(author_link)["create"] is user_create

    def test_a_named_where_can_be_optional_and_list_its_fields(self, orm, User, seed):
        upsert_input = orm.mutations.upsert_node_input(
            model=User, project=_named_user_project(), name="FurnishUserWhereInput"
        )
        where_input = _field_types(upsert_input)["where"]
        assert _optional_fields(where_input) == {"id": True, "email": True}

    @pytest.mark.parametrize(
        ("meta", "message"),
        [
            ({"name": ""}, "_meta.name .* must be a non-empty string"),
            (
                {"upsert": {"where": ["email"], "optional": "yes"}},
                "_meta.upsert.optional .* must be a boolean",
            ),
        ],
    )
    def test_a_malformed_name_or_optional_is_rejected(
        self, orm, User, seed, meta, message
    ):
        with pytest.raises(ValueError, match=message):
            orm.mutations._normalize_model_project(User, {"_meta": meta})

    def test_a_recursive_project_without_a_name_is_rejected(self, orm, User, seed):
        user: dict = {"_meta": {"upsert": {"where": ["email"]}}}
        user["posts"] = {"_meta": {"create": True}, "author": user}
        with pytest.raises(ValueError, match="requires _meta.name"):
            orm.mutations.upsert_node_input(
                model=User, project=user, name="UnnamedRecursiveUser"
            )


def _named_user_project() -> dict:
    user: dict = {
        "_meta": {
            "name": "FurnishUser",
            "create": True,
            "update": True,
            "upsert": {"where": ["id", "email"], "optional": True},
        }
    }
    post = {
        "_meta": {"name": "FurnishPost", "create": True, "update": True},
        "author": user,
    }
    user["posts"] = post
    return user


def _field_types(input_type: type) -> dict[str, Any]:
    return {
        field.python_name: _unwrap(field.type)
        for field in input_type.__strawberry_definition__.fields
    }


def _unwrap(field_type: Any) -> Any:
    while hasattr(field_type, "of_type"):
        field_type = field_type.of_type
    return field_type


class AbstractTestNodeMutationInputs(UpsertNodeInputCases):
    def test_the_create_input_carries_a_nested_relation(self, node_execute, seed):
        data = node_execute(CREATE_WITH_NESTED_RELATION)
        assert data == {"inspectCreateNodeInput": "post"}

    def test_the_update_input_carries_a_nested_relation(self, node_execute, seed):
        data = node_execute(UPDATE_WITH_NESTED_RELATION)
        assert data == {"inspectUpdateNodeInput": "comment"}

    def test_a_projected_input_accepts_the_declared_branch(self, node_execute, seed):
        data = node_execute(PROJECTED_DECLARED_BRANCH)
        assert data == {"inspectProjectedCreateNodeInput": "post"}

    def test_a_projected_input_stops_at_the_declared_depth(
        self, node_execute_result, seed
    ):
        result = node_execute_result(PROJECTED_TOO_DEEP)
        assert result.errors is not None
        message = str(result.errors[0])
        assert "posts" in message and (
            "not defined" in message or "unknown field" in message.lower()
        )

    def test_a_projected_input_leaves_out_undeclared_relations(
        self, node_execute_result, seed
    ):
        result = node_execute_result(PROJECTED_UNDECLARED_RELATION)
        assert result.errors is not None
        message = str(result.errors[0])
        assert "post" in message and (
            "not defined" in message or "unknown field" in message.lower()
        )

    def test_a_projected_update_input_selects_its_root(self, node_execute, seed):
        data = node_execute(PROJECTED_UPDATE)
        assert data == {"inspectProjectedUpdateNodeInput": "comment"}

    def test_an_unknown_projection_relation_is_rejected(self, orm, Post, seed):
        @orm.type(Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        with pytest.raises(ValueError, match="Unknown relation 'not_a_relation'"):
            orm.mutations.create_node_input(
                project={"post": {"not_a_relation": {}}},
                name="BadProjectionInput",
            )
