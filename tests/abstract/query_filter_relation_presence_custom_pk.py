"""FK presence when the related model uses a non-``id`` primary key."""


class AbstractTestQueryFilterRelationPresenceCustomPk:
    """Uses ``custom_pk_execute`` and ``custom_pk_orm`` fixtures per backend."""

    def test_fk_presence_when_related_pk_is_not_id(self, custom_pk_execute):
        data = custom_pk_execute("""
            { books(filter: {
                object: { publisher: { isNull: false } }
            }) { title } }
        """)
        titles = sorted(b["title"] for b in data["books"])
        assert titles == ["Dune", "Neuromancer"]

    def test_filter_related_by_custom_pk_field(self, custom_pk_execute):
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

    def test_publisher_filter_exposes_custom_pk_field(self, custom_pk_orm, Publisher):
        from strawberry_orm.filters import is_reference_lookup_type

        PublisherFilter = custom_pk_orm.filter(Publisher)
        field_type = PublisherFilter._field_type
        definition = field_type.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "publisher_code" in field_names
        assert is_reference_lookup_type(
            next(f.type for f in definition.fields if f.name == "publisher_code")
        )
        assert "id" not in field_names

    def test_book_filter_hides_forward_fk_column(self, custom_pk_orm, Book):
        BookFilter = custom_pk_orm.filter(Book)
        field_type = BookFilter._field_type
        field_names = {f.name for f in field_type.__strawberry_definition__.fields}
        assert "publisher_id" not in field_names
        assert "publisherId" not in field_names

    def test_book_filter_type_relation_auto(self, custom_pk_execute):
        data = custom_pk_execute("""
            { books(filter: {
                object: { publisher: { isNull: false } }
            }) { title publisher { publisherCode } } }
        """)
        assert len(data["books"]) == 2
        codes = {b["publisher"]["publisherCode"] for b in data["books"]}
        assert codes == {"ACE", "PEN"}
