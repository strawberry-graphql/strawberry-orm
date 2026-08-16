"""``on=`` is refused on Tortoise, because it could not be eager here.

Tortoise implements neither a ``to_attr`` equivalent, which would let a second
view of a relation be prefetched beside the first, nor
``split_parent_predicate``, without which the batcher cannot collapse the
per-parent queries. The field would therefore cost one query per parent row,
which is what ``orm.field.lazy`` is for.
"""

import pytest

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


def _orm():
    return StrawberryORM.for_tortoise(warn_missing_scope=False, lazy_resolution="off")


class TestViaIsRefused:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        """Tortoise has to be initialised before relations are introspectable."""

    def test_on_is_refused_with_a_message_naming_the_alternative(self):
        orm = _orm()

        @orm.type(TPost)
        class PostType:
            id: auto
            title: auto

        with pytest.raises(ValueError, match="on= cannot be eager on this backend"):

            @orm.type(TUser)
            class UserType:
                id: auto
                published: list[PostType] = orm.field.eager(
                    on="posts", scope=lambda qs, info: qs.filter(is_published=True)
                )

    def test_the_message_points_at_lazy(self):
        orm = _orm()

        @orm.type(TPost)
        class PostType:
            id: auto

        with pytest.raises(ValueError, match="orm.field.lazy"):

            @orm.type(TUser)
            class UserType:
                id: auto
                published: list[PostType] = orm.field.eager(on="posts")
