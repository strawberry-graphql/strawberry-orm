"""ReferenceLookup tests for SQLAlchemy custom-PK models."""

from tests.backends.sqlalchemy.custom_pk_fixtures import (  # noqa: F401
    custom_pk_execute,
    custom_pk_seed,
)


class TestQueryFilterReferenceLookupCustomPk:
    def test_filter_books_by_object_publisher_code(self, custom_pk_execute):  # noqa: F811
        data = custom_pk_execute("""
            { books(filter: {
                object: {
                    publisher: {
                        field: { publisherCode: { exact: "ACE" } }
                    }
                }
            }) { title } }
        """)
        assert data == {"books": [{"title": "Neuromancer"}]}
