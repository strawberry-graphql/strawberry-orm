"""Tortoise reads every parent's relation on its own, whatever its query holds."""

import pytest
from tortoise.functions import Count
from tortoise.query_utils import Prefetch
from tortoise.queryset import QuerySet

from strawberry_orm import StrawberryORM
from tests.abstract.batching_query_state import AbstractTestBatchingQueryState
from tests.backends.tortoise.models import Comment, Post


class WkLabelledQuerySet(QuerySet):
    pass


def posts(pk: int) -> QuerySet:
    return Post.filter(author_id=pk)


BUILDS = {
    "prefetch_lookup": lambda pk, mine, viewer: posts(pk).prefetch_related(
        *(["comments"] if mine else [])
    ),
    "prefetch_to_attr": lambda pk, mine, viewer: posts(pk).prefetch_related(
        Prefetch(
            "comments",
            Comment.all() if mine else Comment.filter(author_id=viewer),
            to_attr="wk_shown",
        )
    ),
    "select_related": lambda pk, mine, viewer: (
        posts(pk).select_related("author") if mine else posts(pk)
    ),
    "values": lambda pk, mine, viewer: posts(pk) if mine else posts(pk).values(),
    "annotation": lambda pk, mine, viewer: (
        posts(pk).annotate(wk=Count("comments")) if mine else posts(pk)
    ),
    "queryset_class": lambda pk, mine, viewer: (
        posts(pk) if mine else WkLabelledQuerySet(Post).filter(author_id=pk)
    ),
}


@pytest.fixture
def backend():
    return StrawberryORM.for_tortoise(lazy_resolution="off")._backend


@pytest.fixture(params=sorted(BUILDS))
def state(request):
    return request.param


@pytest.fixture(params=[])
def shared(request):
    """Tortoise batches no relation reads, whatever the siblings share."""


@pytest.fixture(params=[])
def observed(request):
    """Without batching each parent's read is already its own."""


@pytest.fixture
def read():
    """Unused: ``observed`` names no state."""


class TestBatchingQueryState(AbstractTestBatchingQueryState):
    builds = BUILDS
