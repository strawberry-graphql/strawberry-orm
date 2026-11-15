"""Ref-list mutation tests for the Tortoise backend."""

import pytest


def _sort_tags(data):
    data["setPostTags"]["tags"] = sorted(
        data["setPostTags"]["tags"],
        key=lambda t: t["name"],
    )
    return data


def test_a_connection_lost_during_a_write_does_not_name_the_host():
    from tortoise.exceptions import DBConnectionError

    from strawberry_orm.backends.tortoise import _write_errors

    lost = DBConnectionError("could not connect to db.internal:5432")
    with (
        pytest.raises(ConnectionError, match="^Database connection failed$") as caught,
        _write_errors(),
    ):
        raise lost
    assert isinstance(caught.value.__cause__, DBConnectionError)


REFUSED_INSERT = """
    mutation {
        setPostTags(postId: 1, tags: [{ create: { name: "python" } }]) { title }
    }
"""


class TestMutationRefList:
    @pytest.mark.asyncio
    async def test_a_refused_insert_does_not_reveal_the_table(
        self, execute_result, seed
    ):
        result = await execute_result(REFUSED_INSERT)
        assert [error.message for error in result.errors] == [
            "The database refused the write"
        ]
        assert result.errors[0].original_error.__cause__ is not None

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
