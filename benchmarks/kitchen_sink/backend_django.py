"""Django models, seeding and execution for the kitchen-sink benchmark."""

import os
import pathlib

import django
from django.conf import settings

DB_DIR = pathlib.Path(os.environ.get("KS_DB_DIR", "/tmp/ks_bench"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "django.sqlite3"

settings.configure(
    DATABASES={
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(DB_PATH)}
    },
    INSTALLED_APPS=["benchmarks.kitchen_sink.dj.KitchenSinkConfig"],
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    SECRET_KEY="bench",
    USE_TZ=False,
    DEBUG=False,
)
django.setup()

from django.db import connection, connections, models  # noqa: E402

from benchmarks.kitchen_sink.harness import timed_sqlite_cursor  # noqa: E402
from strawberry_orm import StrawberryORM  # noqa: E402


class Organization(models.Model):
    name = models.CharField(max_length=100)
    country = models.CharField(max_length=2)
    founded = models.DateField()

    class Meta:
        app_label = "ks"
        db_table = "organization"


class User(models.Model):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="users"
    )
    name = models.CharField(max_length=100)
    email = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    score = models.IntegerField(default=0)
    joined_at = models.DateTimeField()

    class Meta:
        app_label = "ks"
        db_table = "user"


class Category(models.Model):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, related_name="children"
    )

    class Meta:
        app_label = "ks"
        db_table = "category"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "ks"
        db_table = "tag"


class Post(models.Model):
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="posts"
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    status = models.CharField(max_length=20)
    views = models.IntegerField(default=0)
    rating = models.FloatField(default=0)
    published_at = models.DateTimeField(null=True)
    tags = models.ManyToManyField(Tag, related_name="posts", db_table="post_tag")

    class Meta:
        app_label = "ks"
        db_table = "post"


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    upvotes = models.IntegerField(default=0)
    is_flagged = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        app_label = "ks"
        db_table = "comment"


orm = StrawberryORM.for_django(
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
    return Comment.objects.filter(post_id=post.id, upvotes__gte=5).order_by(
        "-upvotes", "id"
    )


def posts_by(user):
    return Post.objects.filter(author_id=user.id)


# -- seeding ------------------------------------------------------------------

MODELS = (Organization, User, Category, Tag, Post, Comment)


def is_seeded(expected_posts: int) -> bool:
    try:
        return Post.objects.count() == expected_posts
    except Exception:
        return False


def seed(data) -> None:
    with connection.cursor() as cursor:
        for table in (
            "comment",
            "post_tag",
            "post",
            "tag",
            "category",
            "user",
            "organization",
        ):
            cursor.execute(f'DROP TABLE IF EXISTS "{table}"')
    with connection.schema_editor() as editor:
        for model in MODELS:
            editor.create_model(model)
    Organization.objects.bulk_create(Organization(**row) for row in data.organizations)
    User.objects.bulk_create((User(**row) for row in data.users), batch_size=500)
    Category.objects.bulk_create(Category(**row) for row in data.categories)
    Tag.objects.bulk_create(Tag(**row) for row in data.tags)
    Post.objects.bulk_create((Post(**row) for row in data.posts), batch_size=500)
    through = Post.tags.through
    through.objects.bulk_create(
        (through(post_id=p, tag_id=t) for p, t in data.post_tags), batch_size=1000
    )
    Comment.objects.bulk_create(
        (Comment(**row) for row in data.comments), batch_size=500
    )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")


# -- statement capture ----------------------------------------------------------


def install_recorder() -> None:
    from django.db.backends.sqlite3 import base

    timed = timed_sqlite_cursor(base.SQLiteCursorWrapper)
    base.SQLiteCursorWrapper = timed
    for conn in connections.all():
        conn.close()


# -- execution ----------------------------------------------------------------


def make_runner(schema, mode: str):
    """``(kind, run)``: run(workload) returns the result's data."""
    if mode == "sync":

        def run(workload):
            result = schema.execute_sync(
                workload.query, variable_values=workload.variables, context_value={}
            )
            if result.errors:
                raise RuntimeError(result.errors)
            return result.data

        return "sync", run

    async def run_async(workload):
        result = await schema.execute(
            workload.query, variable_values=workload.variables, context_value={}
        )
        if result.errors:
            raise RuntimeError(result.errors)
        return result.data

    return "async", run_async
