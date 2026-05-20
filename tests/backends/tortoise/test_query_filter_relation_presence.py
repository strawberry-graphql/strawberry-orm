"""FK presence filter tests for Tortoise."""

import pytest


class TestQueryFilterRelationPresence:
    @pytest.mark.asyncio
    async def test_object_relation_is_null_false(self, execute, seed):
        data = await execute("""
            { posts(filter: {
                object: { author: { isNull: false } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == [
            "Draft Post",
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]

    @pytest.mark.asyncio
    async def test_object_relation_is_null_true_on_nullable_fk(self, execute, seed):
        data = await execute("""
            { comments(filter: {
                object: { parent: { isNull: true } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Great guide", "Nice post!"]

    @pytest.mark.asyncio
    async def test_object_relation_is_null_false_on_nullable_fk(self, execute, seed):
        data = await execute("""
            { comments(filter: {
                object: { parent: { isNull: false } }
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Thanks!"}]}

    @pytest.mark.asyncio
    async def test_object_relation_is_null_inside_all(self, execute, seed):
        data = await execute("""
            { comments(filter: {
                all: [
                    { object: { parent: { isNull: true } } },
                    { field: { body: { contains: "guide" } } }
                ]
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Great guide"}]}

    @pytest.mark.asyncio
    async def test_object_relation_is_null_not(self, execute, seed):
        data = await execute("""
            { comments(filter: {
                not: { object: { parent: { isNull: true } } }
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Thanks!"}]}

    @pytest.mark.asyncio
    async def test_object_relation_is_null_with_field_predicate(self, execute, seed):
        data = await execute("""
            { posts(filter: {
                all: [
                    { object: { author: { isNull: false } } },
                    { object: { author: { field: { name: { exact: "Alice" } } } } }
                ]
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    @pytest.mark.asyncio
    async def test_filter_type_relation_is_null(self, execute, seed):
        data = await execute("""
            { comments(filter: {
                object: { parent: { isNull: true } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Great guide", "Nice post!"]

    @pytest.mark.asyncio
    async def test_root_is_null_raises(self, execute):
        with pytest.raises(AssertionError, match="is_null|isNull|GraphQL errors"):
            await execute("{ posts(filter: { isNull: false }) { title } }")

    def test_filter_schema_exposes_is_null_on_relation_filter(self, orm, User):
        UserFilter = orm.filter(User)
        definition = UserFilter.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "is_null" in field_names


@pytest.mark.usefixtures("custom_pk_db", "custom_pk_seed")
class TestQueryFilterRelationPresenceCustomPk:
    @pytest.mark.asyncio
    async def test_fk_presence_when_related_pk_is_not_id(self, custom_pk_execute):
        data = await custom_pk_execute("""
            { books(filter: {
                object: { publisher: { isNull: false } }
            }) { title } }
        """)
        titles = sorted(b["title"] for b in data["books"])
        assert titles == ["Dune", "Neuromancer"]

    @pytest.mark.asyncio
    async def test_filter_related_by_custom_pk_field(self, custom_pk_execute):
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

    def test_publisher_filter_exposes_custom_pk_field(self, custom_pk_orm, Publisher):
        PublisherFilter = custom_pk_orm.filter(Publisher)
        field_type = PublisherFilter._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "publisher_code" in field_names
        assert "id" not in field_names

    @pytest.mark.asyncio
    async def test_book_filter_type_relation_auto(self, custom_pk_execute):
        data = await custom_pk_execute("""
            { books(filter: {
                object: { publisher: { isNull: false } }
            }) { title publisher { publisherCode } } }
        """)
        assert len(data["books"]) == 2
        codes = {b["publisher"]["publisherCode"] for b in data["books"]}
        assert codes == {"ACE", "PEN"}
