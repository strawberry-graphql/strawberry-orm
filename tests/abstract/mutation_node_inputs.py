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

import pytest
from strawberry import relay

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


class AbstractTestNodeMutationInputs:
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
        assert "Field 'posts' is not defined by type" in str(result.errors[0])

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
