"""``SbRestaurant`` and ``SbBar`` are multi-table children of ``SbPlace``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from django.db import connection, models

from tests.abstract.write_sibling_rows import (
    AbstractTestARowOfTwoSiblingsIsWrittenAsBoth,
)
from tests.backends.django.test_write_path_authorization import _on_db_thread
from tests.backends.django.test_write_path_subclass_rows import SubclassWriteDb


class SbGuide(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "sb_guide"


class SbPlace(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "sb_place"


class SbRestaurant(SbPlace):
    guide = models.ForeignKey(
        SbGuide, null=True, on_delete=models.SET_NULL, related_name="restaurants"
    )

    class Meta:
        app_label = "testapp"
        db_table = "sb_restaurant"


class SbBar(SbPlace):
    # Deleting a restaurant deletes its place row, which Django's collector
    # does not follow down to the bar's.
    place_ptr = models.OneToOneField(
        SbPlace, parent_link=True, primary_key=True, on_delete=models.DB_CASCADE
    )

    class Meta:
        app_label = "testapp"
        db_table = "sb_bar"


class SbReview(models.Model):
    label = models.CharField(max_length=50)
    restaurant = models.ForeignKey(
        SbRestaurant, on_delete=models.CASCADE, related_name="reviews"
    )

    class Meta:
        app_label = "testapp"
        db_table = "sb_review"


class SbPhoto(models.Model):
    label = models.CharField(max_length=50)
    place = models.ForeignKey(SbPlace, on_delete=models.CASCADE, related_name="photos")

    class Meta:
        app_label = "testapp"
        db_table = "sb_photo"


MODELS = SimpleNamespace(
    Guide=SbGuide,
    Place=SbPlace,
    Restaurant=SbRestaurant,
    Bar=SbBar,
    Review=SbReview,
    Photo=SbPhoto,
)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in vars(MODELS).values():
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return SubclassWriteDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestARowOfTwoSiblingsIsWrittenAsBoth(
    AbstractTestARowOfTwoSiblingsIsWrittenAsBoth
):
    pass
