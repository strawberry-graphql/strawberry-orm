"""Async shared tests for createNode/updateNode graph mutations."""

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class AbstractTestNodeGraphMutationsAsync:
    @pytest.mark.asyncio
    async def test_create_node_builds_nested_graph(self, node_execute, seed):
        data = await node_execute(
            """
            mutation {
                createNode(input: {
                    post: {
                        title: "Node Post"
                        body: "Node Body"
                        author: {
                            create: {
                                name: "Dana"
                                email: "dana@example.com"
                            }
                        }
                        tags: [{ create: { name: "node-tag" } }]
                        comments: [{
                            create: {
                                body: "Root comment"
                                author: {
                                    create: {
                                        name: "Eve"
                                        email: "eve@example.com"
                                    }
                                }
                            }
                        }]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        author { name }
                        tags { name }
                        comments { body }
                    }
                }
            }
            """
        )
        assert data == {
            "createNode": {
                "__typename": "PostNode",
                "title": "Node Post",
                "author": {"name": "Dana"},
                "tags": [{"name": "node-tag"}],
                "comments": [{"body": "Root comment"}],
            }
        }

        query_data = await node_execute(
            """
            {
                users { name }
                comments {
                    body
                    author { name }
                }
                tags { name }
            }
            """
        )
        assert {"name": "Dana"} in query_data["users"]
        assert {"name": "Eve"} in query_data["users"]
        assert {"body": "Root comment", "author": {"name": "Eve"}} in query_data[
            "comments"
        ]
        assert {"name": "node-tag"} in query_data["tags"]

    @pytest.mark.asyncio
    async def test_update_node_supports_explicit_nested_update(
        self, node_execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    comment: {
                        id: "1"
                        body: "Nice post updated"
                        author: {
                            update: {
                                id: "2"
                                name: "Bobby"
                            }
                        }
                    }
                }) {
                    __typename
                    ... on CommentNode {
                        body
                        author { name }
                    }
                }
            }
            """
        )
        assert data == {
            "updateNode": {
                "__typename": "CommentNode",
                "body": "Nice post updated",
                "author": {"name": "Bobby"},
            }
        }

        query_data = await node_execute(
            """
            {
                users { name }
                comments {
                    body
                    author { name }
                }
            }
            """
        )
        assert {"name": "Bobby"} in query_data["users"]
        assert {"body": "Nice post updated", "author": {"name": "Bobby"}} in query_data[
            "comments"
        ]

    @pytest.mark.asyncio
    async def test_update_node_m2m_tags_delete(self, node_execute, seed):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "4"
                        tags: [{ delete: { id: "3" } }]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        tags { name }
                    }
                }
            }
            """
        )
        assert data == {
            "updateNode": {
                "__typename": "PostNode",
                "title": "Rust Adventures",
                "tags": [],
            }
        }

        query_data = await node_execute("{ tags { name } }")
        tag_names = {entry["name"] for entry in query_data["tags"]}
        assert "rust" not in tag_names

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_can_link_existing_child(
        self, node_execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "1"
                        comments: [{ update: { id: "3" } }]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        comments { body }
                    }
                }
            }
            """
        )
        assert data["updateNode"]["__typename"] == "PostNode"
        comment_bodies = {comment["body"] for comment in data["updateNode"]["comments"]}
        assert "Great guide" in comment_bodies

        query_data = await node_execute("{ posts { title comments { body } } }")
        posts = {
            post["title"]: {comment["body"] for comment in post["comments"]}
            for post in query_data["posts"]
        }
        assert "Great guide" in posts["Hello World"]
        assert "Great guide" not in posts["GraphQL Guide"]

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_can_update_existing_child(
        self, node_execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "1"
                        comments: [{
                            update: {
                                id: "1"
                                body: "Nice post through post"
                            }
                        }]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        comments { body }
                    }
                }
            }
            """
        )
        assert data["updateNode"]["__typename"] == "PostNode"
        assert {comment["body"] for comment in data["updateNode"]["comments"]} >= {
            "Nice post through post"
        }

        query_data = await node_execute("{ comments { body } }")
        assert {"body": "Nice post through post"} in query_data["comments"]

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_delete_hard_deletes_child(
        self, node_execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "2"
                        comments: [{ delete: { id: "3" } }]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        comments { body }
                    }
                }
            }
            """
        )
        assert data == {
            "updateNode": {
                "__typename": "PostNode",
                "title": "GraphQL Guide",
                "comments": [],
            }
        }

        query_data = await node_execute("{ comments { body } }")
        comment_bodies = {comment["body"] for comment in query_data["comments"]}
        assert "Great guide" not in comment_bodies

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_unlink_rejected_for_non_nullable_relation(
        self, node_execute_result, seed
    ):
        result = await node_execute_result(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "2"
                        comments: [{ unlink: { id: "3" } }]
                    }
                }) {
                    __typename
                }
            }
            """
        )
        assert result.errors is not None
        assert "Cannot detach non-nullable relation 'comments'" in str(result.errors[0])

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_ignores_missing_child_refs(
        self, node_execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    post: {
                        id: "1"
                        comments: [
                            { update: { id: "999" } },
                            { delete: { id: "999" } }
                        ]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        comments { body }
                    }
                }
            }
            """
        )
        assert data == {
            "updateNode": {
                "__typename": "PostNode",
                "title": "Hello World",
                "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
            }
        }

    @pytest.mark.asyncio
    async def test_update_node_reverse_many_can_unlink_nullable_children(
        self, node_execute, execute, seed
    ):
        data = await node_execute(
            """
            mutation {
                updateNode(input: {
                    comment: {
                        id: "1"
                        replies: [{ unlink: { id: "2" } }]
                    }
                }) {
                    __typename
                    ... on CommentNode {
                        body
                    }
                }
            }
            """
        )
        assert data == {
            "updateNode": {
                "__typename": "CommentNode",
                "body": "Nice post!",
            }
        }

        query_data = await execute(
            """
            {
                comments {
                    body
                    parentId
                }
            }
            """
        )
        comments = {
            comment["body"]: comment["parentId"] for comment in query_data["comments"]
        }
        assert comments["Thanks!"] is None

    @pytest.mark.asyncio
    async def test_update_node_missing_root_instance_returns_error(
        self, node_execute_result, seed
    ):
        result = await node_execute_result(
            """
            mutation {
                updateNode(input: {
                    comment: {
                        id: "999"
                        body: "Missing comment"
                    }
                }) {
                    __typename
                }
            }
            """
        )
        assert result.errors is not None
        assert "Comment with id=999 does not exist" in str(result.errors[0])

    @pytest.mark.asyncio
    async def test_projected_create_node_allows_selected_branch(
        self, projected_node_execute, seed
    ):
        data = await projected_node_execute(
            """
            mutation {
                projectedCreateNode(input: {
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
                }) {
                    __typename
                    ... on PostNode {
                        title
                        comments {
                            body
                            author { name }
                        }
                    }
                }
            }
            """
        )
        assert data == {
            "projectedCreateNode": {
                "__typename": "PostNode",
                "title": "Projected Post",
                "comments": [
                    {
                        "body": "Projected Comment",
                        "author": {"name": "Projected Eve"},
                    }
                ],
            }
        }

    @pytest.mark.asyncio
    async def test_projected_update_node_allows_selected_branch(
        self, projected_node_execute, seed
    ):
        data = await projected_node_execute(
            """
            mutation {
                projectedUpdateNode(input: {
                    comment: {
                        id: "1"
                        author: {
                            update: {
                                id: "2"
                                name: "Projected Bob"
                            }
                            onReplace: DISCONNECT
                        }
                    }
                }) {
                    __typename
                    ... on CommentNode {
                        body
                        author { name }
                    }
                }
            }
            """
        )
        assert data == {
            "projectedUpdateNode": {
                "__typename": "CommentNode",
                "body": "Nice post!",
                "author": {"name": "Projected Bob"},
            }
        }

    @pytest.mark.asyncio
    async def test_projected_update_node_uses_fixed_tag_policy(
        self, projected_node_execute, seed
    ):
        data = await projected_node_execute(
            """
            mutation {
                projectedUpdateNode(input: {
                    post: {
                        id: "4"
                        tags: [
                            { create: { name: "projected-replacement-tag" } }
                            { delete: { id: "3" } }
                        ]
                    }
                }) {
                    __typename
                    ... on PostNode {
                        title
                        tags { name }
                    }
                }
            }
            """
        )
        assert data == {
            "projectedUpdateNode": {
                "__typename": "PostNode",
                "title": "Rust Adventures",
                "tags": [{"name": "projected-replacement-tag"}],
            }
        }

        query_data = await projected_node_execute("{ tags { name } }")
        tag_names = {entry["name"] for entry in query_data["tags"]}
        assert "projected-replacement-tag" in tag_names
        assert "rust" not in tag_names

    @pytest.mark.asyncio
    async def test_projected_create_node_blocks_deeper_omitted_branch(
        self, projected_node_execute_result, seed
    ):
        result = await projected_node_execute_result(
            """
            mutation {
                projectedCreateNode(input: {
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
                }) {
                    __typename
                }
            }
            """
        )
        assert result.errors is not None
        assert "Field 'posts' is not defined by type" in str(result.errors[0])

    def test_invalid_projection_key_raises(self, Post, seed):
        orm = StrawberryORM("tortoise")

        @orm.type(Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        with pytest.raises(ValueError, match="Unknown relation 'not_a_relation'"):

            @strawberry.type
            class Mutation:
                create_node = orm.mutations.create_node(
                    project={"post": {"not_a_relation": {}}}
                )
