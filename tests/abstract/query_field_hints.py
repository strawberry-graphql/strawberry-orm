"""Abstract field hints registration tests (backend-agnostic)."""

from strawberry_orm.types import auto


class AbstractTestQueryFieldHintsRegistration:
    def test_load_hint_registered(self, orm, Post, Tag):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list["TT"] = orm.field.auto(using=["author"])

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        hints = orm.backend._store.get("PT", "tags")
        assert hints is not None
        assert hints.using == ["author"]
        assert hints.scope is None

    def test_scope_hint_registered(self, orm, Post, Tag):
        def only_named(qs, info):
            return qs

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list["TT"] = orm.field.auto(scope=only_named)

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        hints = orm.backend._store.get("PT", "tags")
        assert hints is not None
        assert hints.scope is only_named
        assert hints.using is None

    def test_disable_optimization_hint_registered(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto = orm.field.auto(disable_optimization=True)

        hints = orm.backend._store.get("UT", "name")
        assert hints is not None
        assert hints.disable_optimization is True

    def test_computed_field_hint_registered(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return f"by {self.author.name}"

        hints = orm.backend._store.get("PT", "byline")
        assert hints is not None
        assert hints.using == ["author"]

    def test_computed_field_without_hint_registers_empty_hints(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed
            def shout(self) -> str:
                return self.title.upper()

        hints = orm.backend._store.get("PT", "shout")
        assert hints is not None
        assert hints.using is None

    def test_computed_field_only_hint_registered(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

            @orm.field.computed(description="Slug")
            def slug(self) -> str:
                return self.title.lower()

        hints = orm.backend._store.get("PT", "slug")
        assert hints is not None

    def test_compute_hint_registered(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto = orm.field.auto(compute={"name_len": "length(name)"})

        hints = orm.backend._store.get("UT", "name")
        assert hints is not None
        assert hints.compute == {"name_len": "length(name)"}
