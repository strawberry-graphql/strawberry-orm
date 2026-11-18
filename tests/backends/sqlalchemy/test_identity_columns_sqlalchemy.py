"""Identity columns only SQLAlchemy's mapper declares.

A one-to-one held by the related row's foreign key is set from the row
without it: linked to another row, the related row's foreign key moves, and
when that column keys the related row, or picks its model, the row is
rekeyed or retyped. And a subclass can type its rows again, by a column its
base maps: that column picks the model of the base's rows too.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from strawberry_orm import StrawberryORM
from tests.abstract.write_path_authorization import fields_of, member


class Base(DeclarativeBase):
    pass


class IcrHolder(Base):
    __tablename__ = "icr_holder"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    card: Mapped[IcrCard | None] = relationship()
    badge: Mapped[IcrBadge | None] = relationship()


class IcrCard(Base):
    """Keyed by its holder."""

    __tablename__ = "icr_card"

    holder_id: Mapped[int] = mapped_column(
        ForeignKey("icr_holder.id"), primary_key=True
    )
    title: Mapped[str] = mapped_column(String(50))


class IcrBadge(Base):
    __tablename__ = "icr_badge"

    id: Mapped[int] = mapped_column(primary_key=True)
    holder_id: Mapped[int | None] = mapped_column(ForeignKey("icr_holder.id"))
    title: Mapped[str] = mapped_column(String(50))


class IcrKind(Base):
    __tablename__ = "icr_kind"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    item: Mapped[IcrItem | None] = relationship()


class IcrItem(Base):
    """Typed by the row its foreign key links."""

    __tablename__ = "icr_item"
    __mapper_args__ = {"polymorphic_on": "kind_id", "polymorphic_identity": 1}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    kind_id: Mapped[int | None] = mapped_column(ForeignKey("icr_kind.id"))


class IcrSpecial(IcrItem):
    __mapper_args__ = {"polymorphic_identity": 2}


class IcrVehicle(Base):
    __tablename__ = "icr_vehicle"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))
    trim: Mapped[str] = mapped_column(String(20), default="plain")


class IcrCar(IcrVehicle):
    __mapper_args__ = {"polymorphic_identity": "car", "polymorphic_on": "trim"}


class IcrSportsCar(IcrCar):
    __mapper_args__ = {"polymorphic_identity": "sport"}


def new_orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        warn_sensitive=False,
    )


def offered(model: type, kind: str, link: str | None = None) -> set[str]:
    orm, name = new_orm(), model.__name__
    if kind in ("node_create", "node_update"):
        build = getattr(orm.mutations, f"{kind.removeprefix('node_')}_node_input")
        root = build(models=[model], name=f"{name}{kind}")
        return fields_of(member(root, name[:1].lower() + name[1:]))
    meta = {"create": True, "update": True, "upsert": {"where": ["title"]}}
    project: dict = {"_meta": meta}
    if link is not None:
        project[link] = {"_meta": meta}
    try:
        upsert = orm.mutations.upsert_node_input(
            model=model, project=project, name=f"{name}Upsert"
        )
    except ValueError:
        return set()
    return fields_of(member(upsert, kind.removeprefix("upsert_")))


KINDS = ["node_create", "node_update", "upsert_create", "upsert_update"]


class TestAOneToOneKeepsTheRowItHolds:
    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize(
        ("model", "link"),
        [(IcrHolder, "card"), (IcrKind, "item")],
        ids=["keys it", "types it"],
    )
    def test_no_input_offers_a_link_moving_what_keys_or_types_its_row(
        self, model, link, kind
    ):
        assert link not in offered(model, kind, link)

    @pytest.mark.parametrize("kind", KINDS)
    def test_a_link_by_a_plain_foreign_key_is_still_offered(self, kind):
        assert "badge" in offered(IcrHolder, kind, "badge")


class TestASubclassTypingItsRowsAgain:
    @pytest.mark.parametrize("model", [IcrVehicle, IcrCar])
    @pytest.mark.parametrize("exclude_pk", [True, False])
    def test_no_input_offers_what_types_its_rows(self, model, exclude_pk):
        built = new_orm().input(
            model,
            exclude_pk=exclude_pk,
            include=["id", "title", "kind", "trim"],
            name=f"{model.__name__}{exclude_pk}",
        )
        assert not fields_of(built) & {"kind", "trim"}

    @pytest.mark.parametrize("kind", ["upsert_create", "upsert_update"])
    def test_no_upsert_of_the_base_offers_what_types_a_subclass(self, kind):
        assert not offered(IcrVehicle, kind) & {"kind", "trim"}
