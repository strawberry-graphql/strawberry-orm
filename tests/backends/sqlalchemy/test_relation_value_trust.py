import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, create_engine, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    contains_eager,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    subqueryload,
    with_polymorphic,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.util import await_only

from strawberry_orm import StrawberryORM
from tests.abstract.relation_value_trust import AbstractTestRelationValueTrust


def _models(lazy):
    """``Author`` and ``Post`` mapped with ``relationship(lazy=lazy)`` both ways."""

    class Base(DeclarativeBase):
        pass

    class RtAuthor(Base):
        __tablename__ = f"rt_{lazy}_author"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column(String(50))
        is_public: Mapped[bool]
        posts: Mapped[list["RtPost"]] = relationship(back_populates="author", lazy=lazy)

    class RtPost(Base):
        __tablename__ = f"rt_{lazy}_post"

        id: Mapped[int] = mapped_column(primary_key=True)
        title: Mapped[str] = mapped_column(String(50))
        is_published: Mapped[bool]
        author_id: Mapped[int | None] = mapped_column(
            ForeignKey(f"rt_{lazy}_author.id")
        )
        author: Mapped[RtAuthor | None] = relationship(
            back_populates="posts", lazy=lazy
        )

    return SimpleNamespace(Base=Base, Author=RtAuthor, Post=RtPost)


MODELS = {lazy: _models(lazy) for lazy in ("select", "joined", "selectin")}

STAFF_FIRST = (
    "staffP: staffPosts { title author { name posts { title } } } "
    "staffA: staffAuthors { name posts { title } }"
)
PLAIN = "plain: posts { title } plainA: authors { name }"


@dataclass(frozen=True)
class Filler:
    lazy: str = "select"
    #: How the root fields read their rows: ``None`` is the library's field.
    root: str | None = None
    before: str = ""
    after: str = ""
    session: str = "sync"
    optimizer: bool = True
    #: Rows the session already holds, their relations loaded unscoped.
    preload: bool = False


FILLERS = {
    "plain": Filler(),
    "mapper_joined": Filler(lazy="joined"),
    "mapper_selectin": Filler(lazy="selectin"),
    "selectinload": Filler(root="selectinload"),
    "joinedload": Filler(root="joinedload"),
    "subqueryload": Filler(root="subqueryload"),
    "contains_eager": Filler(root="contains_eager"),
    "aliased": Filler(root="aliased"),
    "with_polymorphic": Filler(root="with_polymorphic"),
    "aliased_selectin": Filler(lazy="selectin", root="aliased"),
    "identity_map": Filler(preload=True),
    "pinned": Filler(root="pinned", preload=True),
    "sibling_options": Filler(
        before="warmP: warmPosts { title } warmA: warmAuthors { name }"
    ),
    "sibling_mapper_selectin": Filler(lazy="selectin", before=PLAIN),
    "sibling_staff": Filler(before=STAFF_FIRST),
    "pinned_twice": Filler(root="pinned", preload=True, before=STAFF_FIRST),
    "offload": Filler(session="offload"),
    "offload_sibling_after": Filler(lazy="selectin", session="offload", after=PLAIN),
    "offload_staff": Filler(session="offload", before=STAFF_FIRST),
    "async_session": Filler(session="async"),
    "async_sibling_before": Filler(lazy="selectin", session="async", before=PLAIN),
    "async_pinned": Filler(root="pinned", preload=True, session="async"),
    "optimizer_off": Filler(optimizer=False),
    "optimizer_off_mapper_joined": Filler(lazy="joined", optimizer=False),
    "optimizer_off_pinned": Filler(root="pinned", preload=True, optimizer=False),
}

LOADERS = {
    "selectinload": selectinload,
    "joinedload": joinedload,
    "subqueryload": subqueryload,
}


def _seed(session, models):
    session.add_all(
        [
            models.Author(id=1, name="Alice", is_public=True),
            models.Author(id=2, name="Hidden", is_public=False),
            models.Post(id=1, title="p1", is_published=True, author_id=1),
            models.Post(id=2, title="p2", is_published=True, author_id=2),
            models.Post(id=3, title="draft", is_published=False, author_id=1),
            models.Post(id=4, title="edge", is_published=True, author_id=1),
        ]
    )
    session.commit()


def _preload(session, models):
    """Every row, with both relations loaded as the app's own reads load them."""
    Author, Post = models.Author, models.Post
    posts = session.scalars(
        select(Post).options(selectinload(Post.author).selectinload(Author.posts))
    ).all()
    authors = session.scalars(
        select(Author).options(selectinload(Author.posts).selectinload(Post.author))
    ).all()
    return {
        "posts": [post for post in posts if post.title != "draft"],
        "authors": [author for author in authors if author.name == "Alice"],
    }


@pytest.fixture(params=list(FILLERS))
def filler(request):
    return request.param


@pytest.fixture
def orm(filler):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        offload_sync_session=FILLERS[filler].session == "offload",
    )


class TestRelationValueTrust(AbstractTestRelationValueTrust):
    @pytest_asyncio.fixture(autouse=True)
    async def _session(self, filler, tmp_path):
        spec = FILLERS[filler]
        models = MODELS[spec.lazy]
        self.context = {}
        if spec.session == "async":
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rt.db'}")
            async with engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.create_all)
            session = async_sessionmaker(engine, expire_on_commit=False)()
            await session.run_sync(_seed, models)
            session.expunge_all()
            if spec.preload:
                self.context["pinned"] = await session.run_sync(_preload, models)

            def slower_join(state):
                # The statement carrying the plan's loaders takes longer than
                # its plain sibling, as it would on a networked database.
                if state.statement._with_options and not state.is_relationship_load:
                    await_only(asyncio.sleep(0.05))

            event.listen(session.sync_session, "do_orm_execute", slower_join)
            self.context["session"] = session
            yield
            await session.close()
            await engine.dispose()
            return
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        models.Base.metadata.create_all(engine)
        session = Session(engine)
        _seed(session, models)
        session.expunge_all()
        if spec.preload:
            self.context["pinned"] = _preload(session, models)
        self.context["session"] = session
        yield
        session.close()
        engine.dispose()

    def models_for(self, filler):
        return MODELS[FILLERS[filler].lazy]

    def root(self, filler, models, kind):
        spec = FILLERS[filler]
        Author, Post = models.Author, models.Post
        if kind == "warm_posts":
            return lambda info: select(Post).options(
                selectinload(Post.author).selectinload(Author.posts)
            )
        if kind == "warm_authors":
            return lambda info: select(Author).options(
                selectinload(Author.posts).selectinload(Post.author)
            )
        if spec.root is None:
            return None
        if spec.root == "pinned":
            return lambda info: list(info.context["pinned"][kind])
        model = Post if kind == "posts" else Author
        if spec.root in ("aliased", "with_polymorphic"):
            alias = (
                aliased(model)
                if spec.root == "aliased"
                else with_polymorphic(model, [])
            )
            visible = alias.is_published if kind == "posts" else alias.is_public
            return lambda info: select(alias).where(visible.is_(True))
        relation, back = (
            (Post.author, Author.posts)
            if kind == "posts"
            else (Author.posts, Post.author)
        )
        if spec.root == "contains_eager":
            return lambda info: (
                select(model).outerjoin(relation).options(contains_eager(relation))
            )
        loader = LOADERS[spec.root]
        return lambda info: select(model).options(
            getattr(loader(relation), spec.root)(back)
        )

    def around(self, filler):
        spec = FILLERS[filler]
        return spec.before, spec.after

    def optimizer(self, filler):
        return FILLERS[filler].optimizer

    async def run(self, filler, schema, query):
        if FILLERS[filler].session == "sync":
            # Off the event loop, where a sync app runs it.
            return await asyncio.to_thread(
                schema.execute_sync, query, context_value=dict(self.context)
            )
        return await schema.execute(query, context_value=dict(self.context))

    @staticmethod
    def one(row, name):
        return getattr(row, name)

    @staticmethod
    def many(row, name):
        return list(getattr(row, name))

    @staticmethod
    def public(stmt):
        return stmt.where(stmt.column_descriptions[0]["entity"].is_public.is_(True))

    @staticmethod
    def published(stmt):
        return stmt.where(stmt.column_descriptions[0]["entity"].is_published.is_(True))

    @staticmethod
    def not_edge(stmt):
        return stmt.where(stmt.column_descriptions[0]["entity"].title != "edge")
