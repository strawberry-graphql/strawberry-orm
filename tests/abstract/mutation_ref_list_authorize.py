"""Shared tests for ref-list authorization and hard-delete behavior."""


class AbstractTestRefListAuthorizeSync:
    def test_authorizer_can_block_all_ref_operations(
        self, build_ref_list_authorize_schema, schema_execute, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: False,
            mode="patch",
            hard_delete_removed=False,
        )
        result = schema_execute(
            schema,
            """
            mutation {
                setPostTags(
                    postId: 3
                    tags: [
                        { id: "1" }
                        { create: { name: "blocked-create" } }
                        { update: { id: "1", name: "blocked-update" } }
                        { delete: { id: "1" } }
                    ]
                ) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = schema_execute(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "blocked-create" not in tag_names
        assert "blocked-update" not in tag_names
        assert "python" in tag_names

    def test_patch_delete_can_hard_delete_removed_refs(
        self, build_ref_list_authorize_schema, schema_execute, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: True,
            mode="patch",
            hard_delete_removed=True,
        )
        result = schema_execute(
            schema,
            """
            mutation {
                setPostTags(postId: 1, tags: [{ delete: { id: "1" } }]) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = schema_execute(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "python" not in tag_names

    def test_replace_delete_can_hard_delete_explicit_refs(
        self, build_ref_list_authorize_schema, schema_execute, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: True,
            mode="replace",
            hard_delete_removed=True,
        )
        result = schema_execute(
            schema,
            """
            mutation {
                setPostTags(postId: 2, tags: [{ delete: { id: "2" } }]) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = schema_execute(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "graphql" not in tag_names
        assert "python" not in tag_names


class AbstractTestRefListAuthorizeAsync:
    async def test_authorizer_can_block_all_ref_operations(
        self, build_ref_list_authorize_schema, schema_execute_async, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: False,
            mode="patch",
            hard_delete_removed=False,
        )
        result = await schema_execute_async(
            schema,
            """
            mutation {
                setPostTags(
                    postId: 3
                    tags: [
                        { id: "1" }
                        { create: { name: "blocked-create" } }
                        { update: { id: "1", name: "blocked-update" } }
                        { delete: { id: "1" } }
                    ]
                ) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = await schema_execute_async(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "blocked-create" not in tag_names
        assert "blocked-update" not in tag_names
        assert "python" in tag_names

    async def test_patch_delete_can_hard_delete_removed_refs(
        self, build_ref_list_authorize_schema, schema_execute_async, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: True,
            mode="patch",
            hard_delete_removed=True,
        )
        result = await schema_execute_async(
            schema,
            """
            mutation {
                setPostTags(postId: 1, tags: [{ delete: { id: "1" } }]) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = await schema_execute_async(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "python" not in tag_names

    async def test_replace_delete_can_hard_delete_explicit_refs(
        self, build_ref_list_authorize_schema, schema_execute_async, seed, Post, Tag
    ):
        schema = build_ref_list_authorize_schema(
            Post,
            Tag,
            authorizer=lambda action, model, obj_id, info: True,
            mode="replace",
            hard_delete_removed=True,
        )
        result = await schema_execute_async(
            schema,
            """
            mutation {
                setPostTags(postId: 2, tags: [{ delete: { id: "2" } }]) {
                    name
                }
            }
            """,
        )
        assert result.errors is None
        assert result.data == {"setPostTags": []}

        query_result = await schema_execute_async(schema, "{ tags { name } }")
        assert query_result.errors is None
        tag_names = {tag["name"] for tag in query_result.data["tags"]}
        assert "graphql" not in tag_names
        assert "python" not in tag_names
