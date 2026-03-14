"""Abstract field hints registration tests (backend-agnostic)."""

from strawberry_orm.types import auto


class AbstractTestQueryFieldHintsRegistration:
    def test_load_hint_registered(self, orm, Post, Tag):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list["TT"] = orm.field(load=["author"])

        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        hints = orm.backend._store.get("PT", "tags")
        assert hints is not None
        assert hints.load == ["author"]

    def test_only_hint_registered(self, orm, Post):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            body: auto = orm.field(only=["id", "title"])

        hints = orm.backend._store.get("PT", "body")
        assert hints is not None
        assert hints.only == ["id", "title"]

    def test_disable_optimization_hint_registered(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto = orm.field(disable_optimization=True)

        hints = orm.backend._store.get("UT", "name")
        assert hints is not None
        assert hints.disable_optimization is True

    def test_compute_hint_registered(self, orm, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto = orm.field(compute={"name_len": "length(name)"})

        hints = orm.backend._store.get("UT", "name")
        assert hints is not None
        assert hints.compute == {"name_len": "length(name)"}
