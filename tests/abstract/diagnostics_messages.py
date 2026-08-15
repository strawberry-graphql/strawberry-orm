"""Abstract tests for lazy-resolution diagnostics (backend-agnostic).

The warning has to name the edit that fixes it: which field materialized rows,
and which relation a computed resolver read without declaring it.
"""

import logging

import strawberry

from strawberry_orm.types import auto

LOGGER = "strawberry_orm.lazy_query"


class AbstractTestDiagnosticsMessages:
    """Subclasses provide ``build_schema`` / ``execute`` for their backend."""

    def _capture(self, caplog, schema, query):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        result = self.execute(schema, query)
        assert result.errors is None, result.errors
        records = [r for r in caplog.records if r.name == LOGGER]
        return result, (records[0].message if records else "")

    def test_materializing_ancestor_is_named_once(self, orm, seed, caplog, Post, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            author: UT

        schema = self.build_schema(orm, PT, materialize=True)
        _, message = self._capture(caplog, schema, "{ posts { author { name } } }")

        assert "Unoptimized relation loads detected" in message
        assert message.count("cause:") == 1
        assert "Query.posts returned rows instead of a query object" in message
        assert "fix: return an unexecuted query from Query.posts" in message
        assert "Post.author" in message

    def test_no_cause_when_optimizer_prefetches(self, orm, seed, caplog, Post, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            author: UT

        schema = self.build_schema(orm, PT, materialize=False, optimize=True)
        _, message = self._capture(caplog, schema, "{ posts { author { name } } }")

        assert message == ""

    def test_computed_field_lazy_load_is_reported_with_hint_fix(
        self, orm, seed, caplog, Post
    ):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def byline(self) -> str:
                return f"by {self.author.name}"

        schema = self.build_schema(orm, PT, materialize=True)
        _, message = self._capture(caplog, schema, "{ posts { byline } }")

        assert "PT.byline issued" in message
        assert 'fix: @orm.field.computed(using=["author"])' in message

    def test_computed_field_with_hint_is_silent(self, orm, seed, caplog, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return f"by {self.author.name}"

        schema = self.build_schema(orm, PT, materialize=False, optimize=True)
        result, message = self._capture(caplog, schema, "{ posts { byline } }")

        assert result.data["posts"][0]["byline"] == "by Alice"
        assert message == ""

    def test_probe_is_released_when_a_probed_resolver_raises(self, orm, seed, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def boom(self) -> str:
                raise RuntimeError("resolver exploded")

        schema = self.build_schema(orm, PT, materialize=True)
        failed = self.execute(schema, "{ posts { boom } }")
        assert failed.errors

        # The probe must have been released, so later queries still work.
        recovered = self.execute(schema, "{ posts { title } }")
        assert recovered.errors is None
        assert recovered.data["posts"]

    def test_error_mode_raises_with_the_same_report(self, orm, seed, Post, User):
        import pytest

        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            author: UT

        schema = self.build_schema(orm, PT, materialize=True, mode="error")
        result = self.execute(schema, "{ posts { author { name } } }")

        assert result.errors is not None
        message = str(result.errors[0].message)
        assert "Unoptimized relation loads detected" in message
        assert "cause: Query.posts returned rows" in message

        with pytest.raises(AssertionError):
            assert "no such text" in message

    def test_computed_field_reading_two_relations_lists_both(
        self, orm, seed, caplog, Post
    ):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def summary(self) -> str:
                # Touch two relations; the ORM-specific accessor differs, so go
                # through the backend-agnostic list() of each.
                comments = self.comments
                count = len(
                    list(comments.all() if hasattr(comments, "all") else comments)
                )
                return f"{self.author.name} / {count}"

        schema = self.build_schema(orm, PT, materialize=True)
        _, message = self._capture(caplog, schema, "{ posts { summary } }")

        assert "Post.author" in message
        assert 'using=["author"' in message

    def test_optimized_and_unoptimized_fields_report_only_the_bad_one(
        self, orm, seed, caplog, Post, User
    ):
        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            author: UT

        schema = self.build_schema(orm, PT, materialize=True)
        _, message = self._capture(
            caplog, schema, "{ posts { title author { name } } }"
        )

        assert "Post.author" in message
        assert "Post.title" not in message

    def test_mode_off_is_silent(self, orm, seed, caplog, Post, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            author: UT

        schema = self.build_schema(orm, PT, materialize=True, mode="off")
        _, message = self._capture(caplog, schema, "{ posts { author { name } } }")

        assert message == ""
