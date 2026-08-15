"""Async counterpart of :mod:`tests.abstract.mutation_node_inputs`."""

import pytest
from strawberry import relay

from strawberry_orm.types import auto
from tests.abstract import mutation_node_inputs as sync_cases


class AbstractTestNodeMutationInputsAsync:
    async def test_the_create_input_carries_a_nested_relation(self, node_execute, seed):
        data = await node_execute(sync_cases.CREATE_WITH_NESTED_RELATION)
        assert data == {"inspectCreateNodeInput": "post"}

    async def test_the_update_input_carries_a_nested_relation(self, node_execute, seed):
        data = await node_execute(sync_cases.UPDATE_WITH_NESTED_RELATION)
        assert data == {"inspectUpdateNodeInput": "comment"}

    async def test_a_projected_input_accepts_the_declared_branch(
        self, node_execute, seed
    ):
        data = await node_execute(sync_cases.PROJECTED_DECLARED_BRANCH)
        assert data == {"inspectProjectedCreateNodeInput": "post"}

    async def test_a_projected_input_stops_at_the_declared_depth(
        self, node_execute_result, seed
    ):
        result = await node_execute_result(sync_cases.PROJECTED_TOO_DEEP)
        assert result.errors is not None
        assert "Field 'posts' is not defined by type" in str(result.errors[0])

    async def test_a_projected_update_input_selects_its_root(self, node_execute, seed):
        data = await node_execute(sync_cases.PROJECTED_UPDATE)
        assert data == {"inspectProjectedUpdateNodeInput": "comment"}

    async def test_an_unknown_projection_relation_is_rejected(self, orm, Post, seed):
        @orm.type(Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        with pytest.raises(ValueError, match="Unknown relation 'not_a_relation'"):
            orm.mutations.create_node_input(
                project={"post": {"not_a_relation": {}}},
                name="BadProjectionInputAsync",
            )
