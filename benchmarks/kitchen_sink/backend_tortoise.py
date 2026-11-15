"""Tortoise seeding and execution for the kitchen-sink benchmark.

``init()`` must run (in the event loop) before ``schema`` is imported: Tortoise
wires reverse relations only at init.
"""

import os
import pathlib

from tortoise import Tortoise, connections

from benchmarks.kitchen_sink.harness import RECORDER
from benchmarks.kitchen_sink.tortoise_models import (
    Category,
    Comment,
    Organization,
    Post,
    Tag,
    User,
)
from strawberry_orm import StrawberryORM

DB_DIR = pathlib.Path(os.environ.get("KS_DB_DIR", "/tmp/ks_bench"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "tortoise.sqlite3"

__all__ = ["Category", "Comment", "Organization", "Post", "Tag", "User"]

EAGER_VIEWS = False

orm = StrawberryORM.for_tortoise(
    lazy_resolution="off", warn_missing_scope=False, warn_sensitive=False
)


def active_users(query):
    return query.filter(is_active=True)


def visible_comments(query):
    return query.filter(is_flagged=False)


def visible_posts(query):
    return query.exclude(status="archived")


def newest_first(query, info):
    return query.order_by("-published_at", "id")


def top_comments(post):
    return Comment.filter(post_id=post.id, upvotes__gte=5).order_by("-upvotes", "id")


def posts_by(user):
    return Post.filter(author_id=user.id)


async def init() -> None:
    await Tortoise.init(
        db_url=f"sqlite://{DB_PATH}",
        modules={"models": ["benchmarks.kitchen_sink.tortoise_models"]},
    )


async def close() -> None:
    await Tortoise.close_connections()


async def is_seeded(expected_posts: int) -> bool:
    try:
        return await Post.all().count() == expected_posts
    except Exception:
        return False


async def seed(data) -> None:
    conn = connections.get("default")
    for table in (
        "comment",
        "post_tag",
        "post",
        "tag",
        "category",
        "user",
        "organization",
    ):
        await conn.execute_script(f'DROP TABLE IF EXISTS "{table}"')
    await Tortoise.generate_schemas()
    await Organization.bulk_create([Organization(**row) for row in data.organizations])
    await User.bulk_create([User(**row) for row in data.users], batch_size=500)
    await Category.bulk_create([Category(**row) for row in data.categories])
    await Tag.bulk_create([Tag(**row) for row in data.tags])
    await Post.bulk_create([Post(**row) for row in data.posts], batch_size=500)
    await conn.execute_many(
        "INSERT INTO post_tag (post_id, tag_id) VALUES (?, ?)",
        [list(pair) for pair in data.post_tags],
    )
    await Comment.bulk_create([Comment(**row) for row in data.comments], batch_size=500)
    await conn.execute_script("ANALYZE")


def install_recorder() -> None:
    from tortoise.backends.sqlite.client import SqliteClient

    for name in ("execute_query", "execute_query_dict"):
        original = getattr(SqliteClient, name)

        def make(original):
            async def timed(self, query, values=None):
                RECORDER.begin(query)
                try:
                    return await original(self, query, values)
                finally:
                    RECORDER.end()

            return timed

        setattr(SqliteClient, name, make(original))


def make_runner(schema, mode: str):
    async def run_async(workload):
        result = await schema.execute(
            workload.query, variable_values=workload.variables, context_value={}
        )
        if result.errors:
            raise RuntimeError(result.errors)
        return result.data

    return "async", run_async
