"""Tortoise models for the kitchen-sink benchmark (imported by ``Tortoise.init``)."""

from tortoise import fields
from tortoise.models import Model


class Organization(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    country = fields.CharField(max_length=2)
    founded = fields.DateField()

    class Meta:
        table = "organization"


class User(Model):
    id = fields.IntField(primary_key=True)
    organization = fields.ForeignKeyField(
        "models.Organization", related_name="users", db_index=True
    )
    name = fields.CharField(max_length=100)
    email = fields.CharField(max_length=200)
    is_active = fields.BooleanField(default=True)
    score = fields.IntField(default=0)
    joined_at = fields.DatetimeField()

    class Meta:
        table = "user"


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    parent = fields.ForeignKeyField(
        "models.Category", related_name="children", null=True, db_index=True
    )

    class Meta:
        table = "category"


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)

    class Meta:
        table = "tag"


class Post(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("models.User", related_name="posts", db_index=True)
    category = fields.ForeignKeyField(
        "models.Category", related_name="posts", db_index=True
    )
    title = fields.CharField(max_length=200)
    body = fields.TextField()
    status = fields.CharField(max_length=20)
    views = fields.IntField(default=0)
    rating = fields.FloatField(default=0)
    published_at = fields.DatetimeField(null=True)
    tags = fields.ManyToManyField(
        "models.Tag",
        related_name="posts",
        through="post_tag",
        forward_key="tag_id",
        backward_key="post_id",
    )

    class Meta:
        table = "post"


class Comment(Model):
    id = fields.IntField(primary_key=True)
    post = fields.ForeignKeyField("models.Post", related_name="comments", db_index=True)
    author = fields.ForeignKeyField(
        "models.User", related_name="comments", db_index=True
    )
    body = fields.TextField()
    upvotes = fields.IntField(default=0)
    is_flagged = fields.BooleanField(default=False)
    created_at = fields.DatetimeField()

    class Meta:
        table = "comment"
