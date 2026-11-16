"""Models joined one-to-one -- equivalent to the SQLAlchemy and Tortoise ones."""

from django.db import models


class Member(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        app_label = "testapp"
        db_table = "one_to_one_member"


class Profile(models.Model):
    bio = models.CharField(max_length=200)
    is_public = models.BooleanField(default=True)
    member = models.OneToOneField(
        Member, on_delete=models.CASCADE, related_name="profile"
    )

    class Meta:
        app_label = "testapp"
        db_table = "one_to_one_profile"
