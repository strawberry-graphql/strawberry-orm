# Changelog

## 0.24.0

### Added

- **Multi-hop `using=` paths.** `using=["author.posts"]` (or `"author__posts"`) loads every relation on the path with the parent query, on all three backends. Each hop is checked when the type is defined, and an error names the hop that does not exist.

### Fixed

- **`scope_rows` applies to rows a resolver returns already loaded.** Returning a list (`list(qs)`, `.scalars().all()`, `await qs`) instead of a query, or rows whose relations the resolver already prefetched, no longer skips the related type's `scope_rows`: rejected rows are dropped from lists and to-one relations read as `None`, on all three backends. Returning an unsaved instance of a scoped type raises, since it has no primary key to check.
- **Rows loaded through `using=` obey `scope_rows`.** Each hop applies the related type's `scope_rows` and the `scope=` of the field named for that relation, exactly as a selection of it would. Previously a hint loaded its relation unscoped. On Django and Tortoise, selecting the same relation alongside the hinted field returned the hidden row through the selected field.
- A relation reached both by the selection and by a `using=` path is loaded once, so the two can no longer disagree about which rows are cached.
- Tortoise: a scoped relation nested under another scoped relation loaded its rows unscoped. Scoped relations at any depth are now prefetched with their scope in one query per level, instead of falling back to one query per parent row.
- Tortoise: scoped forward relations are loaded in bulk. A hidden row reads as `None` and leaves the foreign key column untouched.
- Tortoise: forward one-to-one relations in the selection are prefetched.

## 0.23.0

### Added

- **Schema tags.** Build several GraphQL schemas from one set of types: tag what belongs to only some of them, then build one per tag with `orm.schema(..., tags="public")`.
  - `tags=` on `orm.field.eager`, `lazy` and `computed`, `orm.connection.*`, `orm.node` and `orm.payload.*`.
  - `tags("a", "b")`, for `metadata=` on anything declared with Strawberry itself: fields, mutations, arguments and input fields.
  - A schema built for some tags keeps what is untagged or carries one of them, and the types that still reach. Building it fails, naming the field, for a hidden argument or input field without a default, and for a type with every field hidden.
- `metadata=` on the same ORM helpers, passed to the Strawberry field.
- `orm.schema(schema_class=...)` builds a `strawberry.Schema` subclass of your own, with the library's extensions mounted.

### Changed

- On an `@orm.type`, a `strawberry.field(metadata=...)` that sets nothing else no longer replaces the model attribute's resolution. It used to stop Django and Tortoise from generating the relation's resolver.

## 0.22.0

### Added

- `orm.mutations.upsert_node_input(model=, project=, name=)` builds a standalone `{where, create, update}` upsert root for one model. `_meta.upsert.where` accepts a field list or `True` for caller-chosen optional fields.
- `_meta.name` names a projection's generated types and lets a projection recurse; `_meta.upsert.optional` makes a listed `where` optional.
- Insert-only (find-or-create) upserts:
  - `_meta.upsert.onMatch` chooses what a matched row does: `"UPDATE"` (the default), `"IGNORE"` or `"APPEND"`.
  - `_meta.immutable: True` removes `update`, `unlink`, `delete` and `onReplace`, and matches with `"APPEND"`.
  - `_meta.upsert.onMatchLinks: "IGNORE"` skips forward links sent for a matched row instead of rejecting them.
  - `_meta.maxVisits` caps how often a model may appear on one nesting path.
  - Contradictory keys raise `ValueError` naming the model and keys.
- `orm.apply_upsert(input, info, *, authorize=None)` executes an upsert input on all three backends as one unit of work: a savepoint on SQLAlchemy (sync and async), `transaction.atomic()` on Django (sync and async), and `in_transaction()` on Tortoise. It validates the whole payload first and raises `UpsertValidationError`, `UpsertNotFoundError` or `PermissionError`.
- `AbstractRepo.find_for_upsert`, `find_for_upsert_async`, `can_upsert` and `on_match`.
- Generated upsert inputs carry `__upsert_policy__`; `__relation_policy__` gains `on_match`, `immutable` and `max_visits`.
- `make_ref_type(..., descriptions=)` sets GraphQL descriptions on its variants.

### Changed

- **`apply_ref_list` now applies the `upsert` arm.** It used to drop it silently. The item is found or created under the parent row and linked, on every backend. On Django and Tortoise, a ref list containing an upsert runs in one transaction, so an error part way leaves no rows.
- **Single links without an `onReplace` field are `@oneOf`.** Previously a payload with several operations was resolved by precedence. This covers update-only links, links whose `onReplace` is narrowed to one option, and links to immutable targets. A link that offers `onReplace` stays a plain input, because `onReplace` is sent alongside `create` or `upsert`.
- A projection with no update operation generates no `*Patch` or `*Update` types. `update_node_input()` raises `ValueError` for a model whose projection has none, instead of building an update branch from every field.
- Projections that use none of the new keys generate the same SDL as before, apart from the `@oneOf` change above.

### Fixed

- `AbstractRepo._create_async` and `_save_async` await the flush on a SQLAlchemy `AsyncSession`, so the row has its primary key when the call returns.
