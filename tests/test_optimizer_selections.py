"""Unit tests for optimizer GraphQL selection walking."""

from graphql import (
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    parse,
)

from strawberry_orm.optimizer.selections import (
    child_field_type,
    field_nodes_from_info,
    fragments_from_info,
    iter_field_nodes,
    registered_type_name,
)


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


class TestFieldNodesFromInfo:
    def test_reads_field_nodes_from_raw_resolve_info(self):
        # Strawberry's ``Info`` exposes the graphql-core resolve info as
        # ``_raw_info`` and, since strawberry-graphql 0.321, no longer carries a
        # ``field_nodes`` attribute of its own.
        doc = parse("{ posts { id } }")
        field_nodes = doc.definitions[0].selection_set.selections
        raw_info = type("RawInfo", (), {"field_nodes": field_nodes})()
        wrapped = type("Info", (), {"_raw_info": raw_info})()
        assert not hasattr(wrapped, "field_nodes")
        assert field_nodes_from_info(wrapped) == list(field_nodes)

    def test_falls_back_to_raw_graphql_resolve_info(self):
        # A bare graphql-core ``GraphQLResolveInfo`` carries ``field_nodes`` directly.
        doc = parse("{ posts { id } }")
        field_nodes = doc.definitions[0].selection_set.selections
        raw_info = type("RawInfo", (), {"field_nodes": field_nodes})()
        assert field_nodes_from_info(raw_info) == list(field_nodes)

    def test_returns_empty_when_missing(self):
        assert field_nodes_from_info(type("Info", (), {})()) == []


class _UserModel:
    pass


def _object_type(name: str, **field_types) -> GraphQLObjectType:
    return GraphQLObjectType(
        name, {key: GraphQLField(value) for key, value in field_types.items()}
    )


_USER_TYPE = _object_type("UserType", id=GraphQLString)
_REGISTRY = {"UserType": _UserModel}


class TestRegisteredTypeName:
    def test_matches_the_named_type_through_wrappers(self):
        wrapped = GraphQLNonNull(GraphQLList(GraphQLNonNull(_USER_TYPE)))
        assert registered_type_name(wrapped, _UserModel, _REGISTRY) == "UserType"

    def test_finds_the_type_inside_a_payload(self):
        payload = _object_type(
            "UserPayload", data=GraphQLList(_USER_TYPE), errors=GraphQLString
        )
        assert registered_type_name(payload, _UserModel, _REGISTRY) == "UserType"

    def test_finds_the_type_inside_a_connection(self):
        edge = _object_type("UserEdge", cursor=GraphQLString, node=_USER_TYPE)
        connection = _object_type("UserConnection", edges=GraphQLList(edge))
        assert registered_type_name(connection, _UserModel, _REGISTRY) == "UserType"

    def test_two_registered_names_at_one_depth_is_ambiguous(self):
        brief = _object_type("UserBrief", id=GraphQLString)
        full = _object_type("UserFull", id=GraphQLString)
        registry = {"UserBrief": _UserModel, "UserFull": _UserModel}
        wrapper = _object_type("Pair", brief=brief, full=full)
        assert registered_type_name(wrapper, _UserModel, registry) is None

    def test_none_type_returns_none(self):
        assert registered_type_name(None, _UserModel, _REGISTRY) is None

    def test_no_match_anywhere_returns_none(self):
        scalar_only = _object_type("Totals", count=GraphQLString)
        assert registered_type_name(scalar_only, _UserModel, _REGISTRY) is None

    def test_repeated_types_are_visited_once(self):
        # Both fields point at the same wrapper, which itself has no match.
        inner = _object_type("Inner", value=GraphQLString)
        outer = _object_type("Outer", first=inner, second=inner)
        assert registered_type_name(outer, _UserModel, _REGISTRY) is None

    def test_search_depth_is_bounded(self):
        nested = _object_type("Level4", user=_USER_TYPE)
        for level in (3, 2, 1, 0):
            nested = _object_type(f"Level{level}", below=nested)
        assert registered_type_name(nested, _UserModel, _REGISTRY) is None


class TestChildFieldType:
    def _schema(self) -> GraphQLSchema:
        query = _object_type("Query", user=_USER_TYPE, title=GraphQLString)
        return GraphQLSchema(query=query)

    def test_resolves_the_field_type(self):
        assert child_field_type(self._schema(), "Query", "user") is _USER_TYPE

    def test_none_schema_returns_none(self):
        assert child_field_type(None, "Query", "user") is None

    def test_none_parent_returns_none(self):
        assert child_field_type(self._schema(), None, "user") is None

    def test_unknown_parent_returns_none(self):
        assert child_field_type(self._schema(), "Missing", "user") is None

    def test_scalar_parent_returns_none(self):
        assert child_field_type(self._schema(), "String", "user") is None

    def test_unknown_field_returns_none(self):
        assert child_field_type(self._schema(), "Query", "missing") is None
