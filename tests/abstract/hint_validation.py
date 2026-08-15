"""Abstract tests for schema-build-time hint validation (backend-agnostic).

A hint that can never resolve is a typo, not a preference, so it is rejected
where it is written rather than silently ignored at query time.
"""

import pytest

from strawberry_orm.types import auto


class AbstractTestHintValidation:
    def test_unknown_hint_raises_with_suggestion(self, orm, Post):
        with pytest.raises(ValueError, match=r"no relation 'athor'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["athor"])
                def byline(self) -> str:
                    return ""

    def test_unknown_hint_suggests_closest_relation(self, orm, Post):
        with pytest.raises(ValueError, match=r"Did you mean 'author'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["athor"])
                def byline(self) -> str:
                    return ""

    def test_unknown_hint_without_close_match_has_no_suggestion(self, orm, Post):
        with pytest.raises(ValueError) as excinfo:

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["zzzzzzzz"])
                def byline(self) -> str:
                    return ""

        assert "Did you mean" not in str(excinfo.value)

    def test_multi_hop_hint_is_rejected(self, orm, Post):
        with pytest.raises(ValueError, match=r"multi-hop using='author__name'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["author__name"])
                def byline(self) -> str:
                    return ""

    def test_dotted_hint_is_rejected(self, orm, Post):
        with pytest.raises(ValueError, match=r"multi-hop using='author.name'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["author.name"])
                def byline(self) -> str:
                    return ""

    def test_field_hint_is_validated_too(self, orm, Post):
        with pytest.raises(ValueError, match=r"no relation 'nope'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto = orm.field.auto(using=["nope"])

    def test_valid_hint_is_accepted(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return ""

        assert orm.backend._store.get("PT", "byline").using == ["author"]

    def test_strict_hints_false_allows_unknown_names(self, orm_factory, Post):
        orm = orm_factory(strict_hints=False)

        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["does_not_exist"])
            def byline(self) -> str:
                return ""

        assert orm.backend._store.get("PT", "byline").using == ["does_not_exist"]

    def test_one_bad_name_among_several_is_reported(self, orm, Post):
        with pytest.raises(ValueError, match=r"no relation 'nope'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["author", "nope", "tags"])
                def mixed(self) -> str:
                    return ""

    def test_a_column_name_is_not_a_valid_hint(self, orm, Post):
        """``title`` exists on the model but is not a relation to load."""
        with pytest.raises(ValueError, match=r"no relation 'title'"):

            @orm.type(Post)
            class PT:
                id: auto
                title: auto

                @orm.field.computed(using=["title"])
                def shout(self) -> str:
                    return ""

    def test_several_valid_hints_are_all_recorded(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author", "tags"])
            def blurb(self) -> str:
                return ""

        assert orm.backend._store.get("PT", "blurb").using == ["author", "tags"]

    def test_scope_only_field_skips_validation(self, orm, Post, Tag):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list["TT"] = orm.field.auto(scope=lambda qs, info: qs)

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        assert orm.backend._store.get("PT", "tags").using is None
