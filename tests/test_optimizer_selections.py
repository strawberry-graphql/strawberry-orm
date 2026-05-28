"""Unit tests for optimizer GraphQL selection walking."""

from graphql import parse

from strawberry_orm.optimizer.selections import fragments_from_info, iter_field_nodes


class TestIterFieldNodes:
    def test_yields_fields_from_inline_fragments(self):
        doc = parse("{ items { ... on ItemA { id name } ... on ItemB { id } } }")
        field_node = doc.definitions[0].selection_set.selections[0]
        names = [
            node.name.value for node in iter_field_nodes(field_node.selection_set, {})
        ]
        assert names == ["id", "name", "id"]

    def test_yields_fields_from_fragment_spread(self):
        doc = parse(
            """
            fragment ItemFields on Item { id title }
            { items { ...ItemFields } }
            """
        )
        fragments = {
            defn.name.value: defn
            for defn in doc.definitions
            if defn.kind == "fragment_definition"
        }
        operation = next(
            defn for defn in doc.definitions if defn.kind == "operation_definition"
        )
        field_node = operation.selection_set.selections[0]
        names = [
            node.name.value
            for node in iter_field_nodes(field_node.selection_set, fragments)
        ]
        assert names == ["id", "title"]

    def test_skips_unknown_fragment_spread(self):
        doc = parse("{ items { ...Missing } }")
        field_node = doc.definitions[0].selection_set.selections[0]
        assert list(iter_field_nodes(field_node.selection_set, {})) == []

    def test_none_selection_set(self):
        assert list(iter_field_nodes(None, {})) == []

    def test_yields_fields_under_relay_edges_and_node(self):
        doc = parse(
            """
            {
              usersConnection {
                edges {
                  cursor
                  node {
                    name
                    posts { title }
                  }
                }
              }
            }
            """
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        names = [
            node.name.value for node in iter_field_nodes(field_node.selection_set, {})
        ]
        assert names == ["name", "posts"]

    def test_skips_page_info_and_items_passthrough_fields(self):
        doc = parse(
            """
            {
              usersConnection {
                pageInfo { hasNextPage }
                edges {
                  node { name }
                  cursor
                }
                groups {
                  key
                  items { edges { node { id } } }
                }
              }
            }
            """
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        names = [
            node.name.value for node in iter_field_nodes(field_node.selection_set, {})
        ]
        assert names == ["hasNextPage", "name", "key", "id"]

    def test_yields_fields_from_fragment_spread_under_relay_node(self):
        doc = parse(
            """
            fragment UserPosts on UserNode {
              posts { title }
            }
            {
              usersConnection {
                edges {
                  node {
                    ...UserPosts
                  }
                }
              }
            }
            """
        )
        fragments = {
            defn.name.value: defn
            for defn in doc.definitions
            if defn.kind == "fragment_definition"
        }
        operation = next(
            defn for defn in doc.definitions if defn.kind == "operation_definition"
        )
        field_node = operation.selection_set.selections[0]
        names = [
            node.name.value
            for node in iter_field_nodes(field_node.selection_set, fragments)
        ]
        assert names == ["posts"]

    def test_fragments_from_info_reads_raw_graphql_resolve_info(self):
        doc = parse(
            """
            fragment PostTagFields on PostType { tags { name } }
            { posts { ...PostTagFields } }
            """
        )
        fragments = {
            defn.name.value: defn
            for defn in doc.definitions
            if defn.kind == "fragment_definition"
        }
        operation = next(
            defn for defn in doc.definitions if defn.kind == "operation_definition"
        )
        field_node = operation.selection_set.selections[0]
        raw_info = type("RawInfo", (), {"fragments": fragments})()
        wrapped = type("Info", (), {"_raw_info": raw_info})()
        assert fragments_from_info(wrapped) == fragments
        names = [
            node.name.value
            for node in iter_field_nodes(
                field_node.selection_set, fragments_from_info(wrapped)
            )
        ]
        assert names == ["tags"]

    def test_yields_fields_from_inline_fragment_under_relay_node(self):
        doc = parse(
            """
            {
              usersConnection {
                edges {
                  node {
                    ... on UserNode {
                      posts { tags { name } }
                    }
                  }
                }
              }
            }
            """
        )
        field_node = doc.definitions[0].selection_set.selections[0]
        names = [
            node.name.value for node in iter_field_nodes(field_node.selection_set, {})
        ]
        assert names == ["posts"]
