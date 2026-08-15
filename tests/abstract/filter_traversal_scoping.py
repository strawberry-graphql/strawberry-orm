"""Filtering and ordering must not reach rows that row scoping hides.

``scope_rows`` on a type is a read control. Reading a relation honours it,
but ``object:`` traversal joins straight to the related table, so a caller
could otherwise confirm values on rows they cannot read - one probe at a time.

Every traversal shape gets its own case because each is translated by a
different branch: a nested filter, the foreign-key shortcut, and ordering.

Backends that execute synchronously inherit
``AbstractTestFilterTraversalScoping``; async ones inherit the ``Async``
variant. Both drive the same queries and expectations.
"""

import pytest

HIDDEN_EMAIL_PREFIX = "bob"
VISIBLE_TITLES = ["GraphQL Guide", "Hello World"]

NESTED_HIDDEN = (
    "{ posts(filter: { object: { author: { field: { email: "
    f'{{ startsWith: "{HIDDEN_EMAIL_PREFIX}" }} }} }} }} }}) {{ title }} }}'
)
NESTED_VISIBLE = (
    "{ posts(filter: { object: { author: { field: { name: "
    '{ exact: "Alice" } } } } }) { title } }'
)
FK_SHORTCUT = (
    "{ posts(filter: { object: { author: { field: { id: "
    "{ exact: %s } } } } }) { title } }"
)
ORDER_TRAVERSAL = (
    "{ posts(order: [{ object: { author: { field: { name: ASC } } } }]) { title } }"
)
ORDER_OWN_COLUMN = "{ posts(order: [{ field: { title: ASC } }]) { title } }"

# Two hops: post -> comments -> author. Only the last hop is scoped, so the
# restriction has to be attached to the full path rather than the first
# relation. Bob commented on "Hello World"; so did Alice.
DEEP_TRAVERSAL = (
    "{ posts(filter: { object: { comments: { object: { author: { field: "
    '{ email: { startsWith: "%s" } } } } } } }) { title } }'
)
# ``any`` wraps the traversal in another branch of the translator.
ANY_COMBINATOR = (
    "{ posts(filter: { any: [{ object: { author: { field: { email: "
    '{ startsWith: "%s" } } } } }] }) { title } }'
)
# Relation presence is translated from the foreign key rather than the related
# table, so it needs scoping too: "has an author" has to mean "has one the
# caller can read". Alice wrote the first two posts; Bob and Charlie are hidden.
PRESENCE = "{ posts(filter: { object: { author: { isNull: %s } } }) { title } }"
NEGATED = (
    "{ posts(filter: { not: { object: { author: { field: { email: "
    '{ startsWith: "%s" } } } } } }) { title } }'
)


def titles(result):
    assert result.errors is None, result.errors
    return sorted(post["title"] for post in result.data["posts"])


def assert_only_alice_is_readable(result):
    assert result.errors is None, result.errors
    assert [user["name"] for user in result.data["users"]] == ["Alice"]


def assert_order_traversal_rejected(result):
    assert result.errors is not None, "ordering into a scoped type was allowed"
    assert "scoped" in str(result.errors[0].message).lower(), result.errors


def assert_sorted_by_author(result):
    """Alice wrote the first two posts, then Bob's draft, then Charlie's."""
    assert result.errors is None, result.errors
    ordered = [post["title"] for post in result.data["posts"]]
    assert set(ordered[:2]) == set(VISIBLE_TITLES)
    assert ordered[2:] == ["Draft Post", "Rust Adventures"]


class AbstractTestFilterTraversalScoping:
    """Subclasses expose ``execute`` plus the two user-id helpers.

    The schema under test scopes the user type to Alice; Bob is unreadable.
    """

    def test_reading_the_relation_is_scoped(self):
        assert_only_alice_is_readable(self.execute("{ users { name } }"))

    def test_nested_field_filter_cannot_probe_a_hidden_row(self):
        result = self.execute(NESTED_HIDDEN)
        assert titles(result) == [], "hidden author was probeable via filter"

    def test_nested_filter_still_works_for_a_visible_row(self):
        assert titles(self.execute(NESTED_VISIBLE)) == VISIBLE_TITLES

    def test_foreign_key_shortcut_cannot_probe_a_hidden_row(self):
        """``object.author.id`` is translated to a plain FK comparison."""
        result = self.execute(FK_SHORTCUT % self.hidden_user_id())
        assert titles(result) == [], "hidden author reachable through its id"

    def test_foreign_key_shortcut_still_works_for_a_visible_row(self):
        result = self.execute(FK_SHORTCUT % self.visible_user_id())
        assert titles(result) == VISIBLE_TITLES

    def test_ordering_on_the_row_itself_still_works(self):
        result = self.execute(ORDER_OWN_COLUMN)
        assert result.errors is None, result.errors
        ordered = [post["title"] for post in result.data["posts"]]
        assert ordered == sorted(ordered)

    def test_two_hop_traversal_cannot_probe_a_hidden_row(self):
        result = self.execute(DEEP_TRAVERSAL % "bob")
        assert titles(result) == [], "hidden commenter probeable two hops out"

    def test_two_hop_traversal_still_works_for_a_visible_row(self):
        result = self.execute(DEEP_TRAVERSAL % "alice")
        assert titles(result) == ["Hello World"]

    def test_any_branch_cannot_probe_a_hidden_row(self):
        result = self.execute(ANY_COMBINATOR % "bob")
        assert titles(result) == [], "combinator branch skipped scoping"

    def test_negation_reveals_nothing_about_hidden_rows(self):
        """A hidden author never matches, so the result cannot vary with the
        probe - which is what stops negation from being an oracle."""
        matching = titles(self.execute(NEGATED % "bob"))
        non_matching = titles(self.execute(NEGATED % "zzz"))
        assert matching == non_matching

    def test_relation_presence_counts_only_readable_rows(self):
        result = self.execute(PRESENCE % "false")
        assert titles(result) == VISIBLE_TITLES

    def test_relation_absence_covers_rows_hidden_from_the_caller(self):
        result = self.execute(PRESENCE % "true")
        assert titles(result) == ["Draft Post", "Rust Adventures"]


class AbstractTestJoinScopedTraversal:
    """A ``scope_rows`` is not always a plain ``WHERE``.

    Subclasses scope the user type with a join ("users who published
    something"), which hides Bob. Lifting only the WHERE clause out of such a
    hook would drop the join and quietly widen the traversal, so this asserts
    the whole scoped query is carried across.
    """

    def test_join_scoped_relation_cannot_be_probed(self):
        result = self.execute(NESTED_HIDDEN)
        assert titles(result) == [], "join in scope_rows was dropped"

    def test_join_scoped_relation_still_matches_readable_rows(self):
        result = self.execute(NESTED_VISIBLE)
        assert titles(result) == VISIBLE_TITLES


class AbstractTestScopedOrderTraversal:
    """Ordering into a scoped type is refused when the schema is built.

    Subclasses provide ``build(allow=None, checked=True)``, which wires an
    order input that can sort posts by their author while the user type is
    scoped to Alice, and ``execute(schema, query)``. ``checked=False`` builds
    through ``strawberry.Schema`` directly, skipping the build-time check.
    """

    def test_schema_build_rejects_ordering_into_a_scoped_type(self):
        with pytest.raises(ValueError, match="Cannot order by"):
            self.build()

    def test_rejection_names_the_override(self):
        with pytest.raises(ValueError, match=r"allow_scoped_ordering=\['author'\]"):
            self.build()

    def test_override_lets_the_schema_build(self):
        assert self.build(allow=["author"]) is not None

    def test_override_sorts_by_the_related_column(self):
        schema = self.build(allow=["author"])
        assert_sorted_by_author(self.execute(schema, ORDER_TRAVERSAL))

    def test_override_keeps_reads_scoped(self):
        """Opting into the sort must not weaken ``scope_rows`` elsewhere."""
        schema = self.build(allow=["author"])
        assert_only_alice_is_readable(self.execute(schema, "{ users { name } }"))

    def test_override_keeps_filter_traversal_scoped(self):
        schema = self.build(allow=["author"])
        assert titles(self.execute(schema, NESTED_HIDDEN)) == []

    def test_override_must_name_a_relation_that_can_be_ordered_through(self):
        with pytest.raises(ValueError, match="allow_scoped_ordering"):
            self.build(allow=["not_a_relation"])

    def test_query_time_backstop_when_the_build_check_is_skipped(self):
        """``strawberry.Schema`` bypasses the build check, so the translator
        still has to refuse."""
        schema = self.build(checked=False)
        assert_order_traversal_rejected(self.execute(schema, ORDER_TRAVERSAL))

    def test_a_permissive_order_type_does_not_widen_a_different_one(self):
        """Two order inputs can exist for one model. The opt-in belongs to the
        input that declared it, not to the model."""
        with pytest.raises(ValueError, match="Cannot order by Comment.author"):
            self.build_with_permissive_sibling()


class AbstractTestScopedOrderTraversalAsync:
    """Async counterpart of :class:`AbstractTestScopedOrderTraversal`."""

    async def test_schema_build_rejects_ordering_into_a_scoped_type(self):
        with pytest.raises(ValueError, match="Cannot order by"):
            self.build()

    async def test_rejection_names_the_override(self):
        with pytest.raises(ValueError, match=r"allow_scoped_ordering=\['author'\]"):
            self.build()

    async def test_override_lets_the_schema_build(self):
        assert self.build(allow=["author"]) is not None

    async def test_override_sorts_by_the_related_column(self):
        schema = self.build(allow=["author"])
        assert_sorted_by_author(await self.execute(schema, ORDER_TRAVERSAL))

    async def test_override_keeps_reads_scoped(self):
        schema = self.build(allow=["author"])
        assert_only_alice_is_readable(await self.execute(schema, "{ users { name } }"))

    async def test_override_keeps_filter_traversal_scoped(self):
        schema = self.build(allow=["author"])
        assert titles(await self.execute(schema, NESTED_HIDDEN)) == []

    async def test_override_must_name_a_relation_that_can_be_ordered_through(self):
        with pytest.raises(ValueError, match="allow_scoped_ordering"):
            self.build(allow=["not_a_relation"])

    async def test_query_time_backstop_when_the_build_check_is_skipped(self):
        schema = self.build(checked=False)
        assert_order_traversal_rejected(await self.execute(schema, ORDER_TRAVERSAL))

    async def test_a_permissive_order_type_does_not_widen_a_different_one(self):
        with pytest.raises(ValueError, match="Cannot order by Comment.author"):
            self.build_with_permissive_sibling()


class AbstractTestJoinScopedTraversalAsync:
    """Async counterpart of :class:`AbstractTestJoinScopedTraversal`."""

    async def test_join_scoped_relation_cannot_be_probed(self):
        result = await self.execute(NESTED_HIDDEN)
        assert titles(result) == [], "join in scope_rows was dropped"

    async def test_join_scoped_relation_still_matches_readable_rows(self):
        result = await self.execute(NESTED_VISIBLE)
        assert titles(result) == VISIBLE_TITLES


class AbstractTestFilterTraversalScopingAsync:
    """Async counterpart; ``execute`` and the id helpers are awaitable."""

    async def test_reading_the_relation_is_scoped(self):
        assert_only_alice_is_readable(await self.execute("{ users { name } }"))

    async def test_nested_field_filter_cannot_probe_a_hidden_row(self):
        result = await self.execute(NESTED_HIDDEN)
        assert titles(result) == [], "hidden author was probeable via filter"

    async def test_nested_filter_still_works_for_a_visible_row(self):
        assert titles(await self.execute(NESTED_VISIBLE)) == VISIBLE_TITLES

    async def test_foreign_key_shortcut_cannot_probe_a_hidden_row(self):
        result = await self.execute(FK_SHORTCUT % await self.hidden_user_id())
        assert titles(result) == [], "hidden author reachable through its id"

    async def test_foreign_key_shortcut_still_works_for_a_visible_row(self):
        result = await self.execute(FK_SHORTCUT % await self.visible_user_id())
        assert titles(result) == VISIBLE_TITLES

    async def test_ordering_on_the_row_itself_still_works(self):
        result = await self.execute(ORDER_OWN_COLUMN)
        assert result.errors is None, result.errors
        ordered = [post["title"] for post in result.data["posts"]]
        assert ordered == sorted(ordered)

    async def test_two_hop_traversal_cannot_probe_a_hidden_row(self):
        result = await self.execute(DEEP_TRAVERSAL % "bob")
        assert titles(result) == [], "hidden commenter probeable two hops out"

    async def test_two_hop_traversal_still_works_for_a_visible_row(self):
        result = await self.execute(DEEP_TRAVERSAL % "alice")
        assert titles(result) == ["Hello World"]

    async def test_any_branch_cannot_probe_a_hidden_row(self):
        result = await self.execute(ANY_COMBINATOR % "bob")
        assert titles(result) == [], "combinator branch skipped scoping"

    async def test_negation_reveals_nothing_about_hidden_rows(self):
        matching = titles(await self.execute(NEGATED % "bob"))
        non_matching = titles(await self.execute(NEGATED % "zzz"))
        assert matching == non_matching

    async def test_relation_presence_counts_only_readable_rows(self):
        result = await self.execute(PRESENCE % "false")
        assert titles(result) == VISIBLE_TITLES

    async def test_relation_absence_covers_rows_hidden_from_the_caller(self):
        result = await self.execute(PRESENCE % "true")
        assert titles(result) == ["Draft Post", "Rust Adventures"]
