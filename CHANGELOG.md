# Changelog

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
