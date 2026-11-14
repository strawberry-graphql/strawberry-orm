"""A date period is cut in Django's current time zone."""

import datetime
from types import SimpleNamespace

from asgiref.sync import sync_to_async
from django.test import override_settings

from strawberry_orm import StrawberryORM
from tests.abstract.grouping_across_relations import _posts, _posts_schema, _summary
from tests.backends.django.models import Post, Tag, User


@sync_to_async
def _date_posts(dates):
    for pk, when in dates.items():
        Post.objects.filter(pk=pk).update(created_at=when)


class TestGroupingInATimeZone:
    async def test_a_period_starts_in_the_current_time_zone(self, seed):
        """02:00 UTC on 1 March is still February in New York."""
        orm = StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)
        models = SimpleNamespace(User=User, Post=Post, Tag=Tag)
        utc = datetime.UTC
        with override_settings(USE_TZ=True, TIME_ZONE="America/New_York"):
            await _date_posts(
                {
                    1: datetime.datetime(2026, 3, 1, 2, 0, tzinfo=utc),
                    2: datetime.datetime(2026, 3, 1, 12, 0, tzinfo=utc),
                    3: datetime.datetime(2026, 3, 1, 15, 0, tzinfo=utc),
                    4: datetime.datetime(2026, 3, 1, 15, 0, tzinfo=utc),
                }
            )
            schema = _posts_schema(
                orm, models, lambda model, name, query: query.exclude(name=name)
            )
            query = _posts(
                "[{field: {createdAt: {interval: MONTH}}}]",
                order="[{field: {createdAt: ASC}}, {field: {id: ASC}}]",
                groups="key { createdAt }",
            )
            result = await schema.execute(query, context_value={})
        assert _summary(result, lambda key: key["createdAt"]) == [
            ("2026-02-01T00:00:00-05:00", 1, [0]),
            ("2026-03-01T00:00:00-05:00", 3, [1, 2, 3]),
        ]
