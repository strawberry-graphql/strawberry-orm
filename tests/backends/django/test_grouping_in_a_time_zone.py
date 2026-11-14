"""A date period is cut in Django's current time zone."""

import datetime
from types import SimpleNamespace

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.test import override_settings
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
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

    @pytest.mark.parametrize(
        ("group_by", "key", "read"),
        [
            (
                "{object: {posts: {field: {createdAt: {interval: MONTH}}}}}",
                "posts { createdAt }",
                lambda key: key["posts"]["createdAt"],
            ),
            (
                "{objects: {posts: {field: {createdAt: {interval: MONTH}}}}}",
                "objects { posts { createdAt } }",
                lambda key: key["objects"]["posts"][0]["createdAt"],
            ),
        ],
    )
    async def test_a_related_rows_period_starts_in_the_current_time_zone(
        self, seed, group_by, key, read
    ):
        """Read through a scoped type, as a related row's own column reads."""
        orm = StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)
        utc = datetime.UTC
        with override_settings(USE_TZ=True, TIME_ZONE="America/New_York"):
            await _date_posts(
                {
                    1: datetime.datetime(2026, 3, 1, 2, 0, tzinfo=utc),
                    2: datetime.datetime(2026, 3, 1, 3, 0, tzinfo=utc),
                    3: datetime.datetime(2026, 3, 1, 15, 0, tzinfo=utc),
                    4: datetime.datetime(2026, 3, 1, 15, 0, tzinfo=utc),
                }
            )

            @orm.type(Post, group=orm.group(Post))
            class PostNode(relay.Node):
                id: relay.NodeID[int]
                created_at: auto

                @classmethod
                def scope_rows(cls, qs, info):
                    return qs

            @orm.type(User, order=orm.order(User), group=orm.group(User))
            class UserNode(relay.Node):
                id: relay.NodeID[int]
                posts: ORMListConnection[PostNode] = orm.connection.eager()

            @strawberry.type
            class Query:
                users: ORMListConnection[UserNode] = orm.connection.eager()

            result = await orm.schema(query=Query).execute(
                f"""{{ users(order: [{{field: {{id: ASC}}}}], groupBy: [{group_by}]) {{
                    groups {{ key {{ {key} }} }} }} }}""",
                context_value={},
            )
        assert result.errors is None, result.errors
        keys = [read(group["key"]) for group in result.data["users"]["groups"]]
        assert keys == ["2026-02-01T00:00:00-05:00", "2026-03-01T00:00:00-05:00"]
