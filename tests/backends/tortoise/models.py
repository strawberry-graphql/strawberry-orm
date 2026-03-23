"""Tortoise test models -- equivalent to the SQLAlchemy and Django models."""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class User(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    email = fields.CharField(max_length=200)
    created_at = fields.DatetimeField(auto_now_add=True)

    posts: fields.ReverseRelation[Post]
    comments: fields.ReverseRelation[Comment]

    class Meta:
        table = "user"


class Post(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=200)
    body = fields.TextField()
    is_published = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    author = fields.ForeignKeyField("models.User", related_name="posts")

    tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField(
        "models.Tag", related_name="posts", through="post_tag"
    )
    comments: fields.ReverseRelation[Comment]

    class Meta:
        table = "post"


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)

    posts: fields.ManyToManyRelation[Post]

    class Meta:
        table = "tag"


class Comment(Model):
    id = fields.IntField(primary_key=True)
    body = fields.TextField()
    post = fields.ForeignKeyField("models.Post", related_name="comments")
    author = fields.ForeignKeyField("models.User", related_name="comments")
    parent = fields.ForeignKeyField("models.Comment", related_name="replies", null=True)

    replies: fields.ReverseRelation[Comment]

    class Meta:
        table = "comment"
