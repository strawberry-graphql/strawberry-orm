import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.mutation_ref_list_authorize import AbstractTestRefListAuthorizeSync


@pytest.fixture
def schema_execute(sa_session):
    def _execute(schema, query):
        return schema.execute_sync(query, context_value={"session": sa_session})

    return _execute


@pytest.fixture
def build_ref_list_authorize_schema():
    def _build(Post, Tag, *, authorizer):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")

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
            def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                session = info.context["session"]
                post = session.get(Post, post_id)
                orm.apply_ref_list(
                    post,
                    "tags",
                    tags,
                    info,
                    authorize=authorizer,
                )
                session.flush()
                return list(post.tags)  # type: ignore[return-value]

        return strawberry.Schema(
            query=Query,
            mutation=Mutation,
            extensions=[orm.optimizer_extension()],
        )

    return _build


class TestRefListAuthorize(AbstractTestRefListAuthorizeSync):
    pass
