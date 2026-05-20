import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.mutation_ref_list_authorize import AbstractTestRefListAuthorizeAsync


@pytest.fixture
def schema_execute_async():
    async def _execute(schema, query):
        return await schema.execute(query)

    return _execute


@pytest.fixture
def build_ref_list_authorize_schema():
    def _build(Post, Tag, *, authorizer):
        orm = StrawberryORM.for_tortoise()

        @strawberry.input
        class CreateTagInput:
            name: str

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        TagRef = orm.ref(
            Tag, create=CreateTagInput, update=UpdateTagInput, unlink=True, delete=True
        )

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
            async def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await Post.get(id=post_id).prefetch_related("tags")
                await orm.apply_ref_list(
                    post,
                    "tags",
                    tags,
                    info,
                    authorize=authorizer,
                )
                await post.fetch_related("tags")
                return list(post.tags)  # type: ignore[return-value]

        return strawberry.Schema(query=Query, mutation=Mutation)

    return _build


@pytest.mark.asyncio
class TestRefListAuthorize(AbstractTestRefListAuthorizeAsync):
    pass
