import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.mutation_ref_list_authorize import AbstractTestRefListAuthorizeSync


@pytest.fixture
def schema_execute():
    def _execute(schema, query):
        return schema.execute_sync(query)

    return _execute


@pytest.fixture
def build_ref_list_authorize_schema():
    def _build(Post, Tag, *, authorizer, mode, hard_delete_removed):
        orm = StrawberryORM("django")

        @strawberry.input
        class CreateTagInput:
            name: str

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str

        TagRef = orm.ref(Tag, create=CreateTagInput, update=UpdateTagInput, delete=True)

        @orm.type(Tag)
        class TagType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field()

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = Post.objects.get(pk=post_id)
                orm.apply_ref_list(
                    post,
                    "tags",
                    tags,
                    info,
                    authorize=authorizer,
                    mode=mode,
                    hard_delete_removed=hard_delete_removed,
                )
                return list(post.tags.order_by("id"))  # type: ignore[return-value]

        return strawberry.Schema(query=Query, mutation=Mutation)

    return _build


class TestRefListAuthorize(AbstractTestRefListAuthorizeSync):
    pass
