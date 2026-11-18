"""``SbRestaurant`` and ``SbBar`` are joined-table subclasses of ``SbPlace``
without a discriminator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.write_sibling_rows import (
    AbstractTestARowOfTwoSiblingsIsWrittenAsBoth,
)
from tests.backends.sqlalchemy.test_write_path_subclass_rows import SubclassWriteDb


class Base(DeclarativeBase):
    pass


class SbGuide(Base):
    __tablename__ = "sb_guide"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    restaurants: Mapped[list[SbRestaurant]] = relationship(
        back_populates="guide", order_by="SbRestaurant.id"
    )


class SbPlace(Base):
    __tablename__ = "sb_place"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    photos: Mapped[list[SbPhoto]] = relationship(back_populates="place")


class SbRestaurant(SbPlace):
    __tablename__ = "sb_restaurant"

    id: Mapped[int] = mapped_column(ForeignKey("sb_place.id"), primary_key=True)
    guide_id: Mapped[int | None] = mapped_column(ForeignKey("sb_guide.id"))
    guide: Mapped[SbGuide | None] = relationship(back_populates="restaurants")


class SbBar(SbPlace):
    __tablename__ = "sb_bar"

    id: Mapped[int] = mapped_column(ForeignKey("sb_place.id"), primary_key=True)


class SbReview(Base):
    __tablename__ = "sb_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("sb_restaurant.id"))
    restaurant: Mapped[SbRestaurant] = relationship()


class SbPhoto(Base):
    __tablename__ = "sb_photo"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))
    place_id: Mapped[int] = mapped_column(ForeignKey("sb_place.id"))
    # Without a discriminator, holding a subclass row takes this.
    place: Mapped[SbPlace] = relationship(
        back_populates="photos", enable_typechecks=False
    )


MODELS = SimpleNamespace(
    Guide=SbGuide,
    Place=SbPlace,
    Restaurant=SbRestaurant,
    Bar=SbBar,
    Review=SbReview,
    Photo=SbPhoto,
)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SubclassWriteDb(MODELS, async_hooks=False, session=session)
        session.close()
        return
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SubclassWriteDb(MODELS, async_hooks=True, session=session)
    await engine.dispose()


class TestARowOfTwoSiblingsIsWrittenAsBoth(
    AbstractTestARowOfTwoSiblingsIsWrittenAsBoth
):
    pass
