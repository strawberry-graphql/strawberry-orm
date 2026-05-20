"""Django test models -- equivalent to the SQLAlchemy and Tortoise models."""

from django.db import models


class User(models.Model):
    name = models.CharField(max_length=100)
    email = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "testapp"
        db_table = "user"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "testapp"
        db_table = "tag"


class Post(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    tags = models.ManyToManyField(Tag, related_name="posts", blank=True)

    class Meta:
        app_label = "testapp"
        db_table = "post"


class Comment(models.Model):
    body = models.TextField()
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, related_name="replies", null=True, blank=True
    )

    class Meta:
        app_label = "testapp"
        db_table = "comment"


class Publisher(models.Model):
    """Related model with a non-``id`` primary key."""

    publisher_code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"
        db_table = "publisher"


class Book(models.Model):
    title = models.CharField(max_length=200)
    publisher = models.ForeignKey(
        Publisher, on_delete=models.CASCADE, related_name="books"
    )
    prequel = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="sequels",
        null=True,
        blank=True,
    )

    class Meta:
        app_label = "testapp"
        db_table = "book"
