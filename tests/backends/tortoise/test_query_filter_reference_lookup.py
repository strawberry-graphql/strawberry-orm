"""ReferenceLookup tests for Tortoise."""

import pytest


class TestQueryFilterReferenceLookup:
    @pytest.mark.asyncio
    async def test_filter_posts_by_object_author_id_in_list(self, execute, seed):
        data = await execute("""
            { posts(filter: {
                object: { author: { field: { id: { inList: [1] } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    @pytest.mark.asyncio
    async def test_filter_posts_by_object_author_id_one_of(self, execute, seed):
        data = await execute("""
            { posts(filter: {
                object: { author: { oneOf: [
                    { field: { id: { exact: 1 } } },
                    { field: { id: { exact: 3 } } }
                ] } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World", "Rust Adventures"]

    @pytest.mark.asyncio
    async def test_object_author_name_still_filters_by_related_field(
        self, execute, seed
    ):
        data = await execute("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Alice" } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]


@pytest.mark.asyncio
class TestQueryFilterReferenceLookupCustomPk:
    async def test_filter_books_by_object_publisher_code(self, custom_pk_execute):
        data = await custom_pk_execute("""
            { books(filter: {
                object: {
                    publisher: {
                        field: { publisherCode: { exact: "ACE" } }
                    }
                }
            }) { title } }
        """)
        assert data == {"books": [{"title": "Neuromancer"}]}
