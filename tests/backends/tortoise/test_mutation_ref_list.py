"""Ref-list mutation tests for the Tortoise backend."""

import pytest


def _sort_tags(data):
    data["setPostTags"]["tags"] = sorted(
        data["setPostTags"]["tags"],
        key=lambda t: t["name"],
    )
    return data


class TestMutationRefList:
    @pytest.mark.asyncio
    async def test_link_existing_tags(self, execute, seed):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 3, tags: [
                    { update: { id: "1" } }, { update: { id: "3" } }
                ]) {
                    title tags { name }
                }
            }
            """
        )
        assert _sort_tags(data) == {
            "setPostTags": {
                "title": "Draft Post",
                "tags": [{"name": "python"}, {"name": "rust"}],
            }
        }

    @pytest.mark.asyncio
    async def test_create_inline_tag(self, execute, seed):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 1, tags: [
                    { update: { id: "1" } },
                    { create: { name: "django" } }
                ]) { tags { name } }
            }
            """
        )
        assert _sort_tags(data) == {
            "setPostTags": {
                "tags": [{"name": "django"}, {"name": "python"}],
            }
        }

    @pytest.mark.asyncio
    async def test_update_inline_tag(self, execute, seed):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [
                    { update: { id: "1" } },
                    { update: { id: "2", name: "gql" } }
                ]) { tags { name } }
            }
            """
        )
        assert _sort_tags(data) == {
            "setPostTags": {
                "tags": [{"name": "gql"}, {"name": "python"}],
            }
        }

    @pytest.mark.asyncio
    async def test_unlink_related_tag(self, execute, seed, Tag):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [
                    { update: { id: "1" } },
                    { unlink: { id: "2" } }
                ]) { tags { name } }
            }
            """
        )
        assert data == {"setPostTags": {"tags": [{"name": "python"}]}}
        tag2 = await Tag.get_or_none(pk=2)
        assert tag2 is not None

    @pytest.mark.asyncio
    async def test_delete_related_tag(self, execute, seed, Tag):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 2, tags: [
                    { update: { id: "1" } },
                    { delete: { id: "2" } }
                ]) { tags { name } }
            }
            """
        )
        assert data == {"setPostTags": {"tags": [{"name": "python"}]}}
        tag2 = await Tag.get_or_none(pk=2)
        assert tag2 is None

    @pytest.mark.asyncio
    async def test_mixed_operations(self, execute, seed):
        data = await execute(
            """
            mutation {
                setPostTags(postId: 1, tags: [
                    { update: { id: "3" } },
                    { create: { name: "fastapi" } },
                    { update: { id: "1", name: "py" } }
                ]) { tags { name } }
            }
            """
        )
        assert _sort_tags(data) == {
            "setPostTags": {
                "tags": [{"name": "fastapi"}, {"name": "py"}, {"name": "rust"}],
            }
        }
