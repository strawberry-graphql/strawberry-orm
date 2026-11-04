# Changelog

## 0.25.0

### Added

- **`strawberry_orm.contrib`**, a home for optional plugins, each installed through its own extra.
- **`strawberry_orm.contrib.mcp`** (`pip install "strawberry-orm[contrib.mcp]"`): `mcp_server(QueryAgent(schema, model="anthropic:…"))` serves a schema as an MCP server with one `ask(question)` tool. A model writes a single GraphQL query for the question, and the query runs through the schema with the caller's context, so `scope_rows` and permissions apply. Mutations are rejected unless `allow_mutations=True`; queries are validated and depth-limited before they run. Models are called through any-llm, so the provider is a string. The rows of the answer never reach the model.
  - Clarifying questions: the model can ask for missing details through `ask_user`, shown as an elicitation form. 2026-07-28 clients get `input_required` with the agent's progress in sealed request state, older clients a form during the call, and clients without forms a `needs_input` result. `clarify=` and `max_clarifications=`.
  - Allowed mutations are confirmed by the person asking first (`confirm_mutations=True`). A paused confirmation runs its change once, within `confirm_within` seconds (default 600), and the change is checked again when it resumes.
  - Opt-in lookups (`lookups=True`): small read-only queries whose rows, capped by `max_lookup_rows`, the model sees. Turning them on with mutations allowed and `confirm_mutations=False` warns, since an instruction hidden in a row another user wrote could then steer the model into an unconfirmed change.
  - Hooks: `on_clarify(clarification, context)` answers or refuses a clarification; `before_query(query, variables, context)` rejects an operation with `QueryRejected` or stops with `QueryAgentError`.
  - `QueryAgent.ask(..., clarifier=)` and `QueryAgent.resume(state, answers)` for use without MCP.

### Changed

- **Filters, ordering, group-by and aggregates cover what the type shows.** They were generated from every column of the model, so a column the type hid could still be filtered on (read one `startsWith` probe at a time), sorted by, returned verbatim as a group key, or read through `min`/`max`. Each now covers only the columns and relations shown by the types it serves: the types it is attached to, and through `object:` the type the relation returns. `orm.schema()` works this out for the types each schema reaches, so an admin schema can keep a column a public schema hides; a schema built with `strawberry.Schema` directly gets the inputs uncut. A field with its own resolver, `on=`, `compute=` or permission classes does not count, since it does not show the column as stored; a `scope=` does, because traversal applies it too. Kept fields carry the type field's tags. **Breaking:** clients filtering, sorting or grouping on a column the type does not show get a validation error; show the column on the type to keep it. This replaces the error raised when a type's `exclude=` and its inputs disagreed.

### Fixed

- A type with `group=` but neither `filters=` nor `order=` could not be listed through `orm.connection()`: the generated resolver declared `filter` and `order` arguments it had no types for, which Strawberry refuses.
- Relation connections handed each parent's page to its resolver on the shared model instance and never removed it. On SQLAlchemy, whose session returns the same instance wherever a row is reached, an alias or a later request in the same session read that page, with the earlier field's filter and the earlier caller's `scope_rows`, instead of its own rows. Each field now takes its own page, and none outlives the field.
- The windowed read behind relation connections and `groups { items }` used `first` and the `after` cursor as a row limit before they were checked. `first: 100000` read every related row before being refused, and a forged `after` cursor read every row and returned empty pages without an error. The window is now computed as Strawberry computes the page, so an out-of-range `first` is refused before any read, and it starts at the page rather than the first row. `first` with `before` now pages as Strawberry defines it, and `items` without `first` returns up to `max_results` rather than eleven rows reported as the last page.

## 0.24.2

### Fixed

- **Rows that tie on a connection's order no longer repeat or go missing between pages.** Since 0.24.1 each page is its own query, and where the order was not unique a database could break ties differently on each page, so a row could appear on two pages and another on none. The cut now ends every order with the primary key, unless it already includes it: the client's `order`, the resolver's, the model's default, or none at all. This applies on all three backends, sync and async, to root connections and to the windowed pages of relation connections and `groups { items }`. Cursors, `pageInfo`, `totalCount` and the order of rows that do not tie are unchanged.
- Relation connections over several parents failed under async Django and with an SQLAlchemy `AsyncSession`: each parent's total was counted synchronously.

## 0.24.1

### Added

- `apply_page(query, offset, limit)` on the backend protocol. A backend returns the query cut to one page, or `None` to have the connection read it whole.

### Fixed

- **Connections read only the page they are asked for.** A connection over a query (`orm.connection.*`, `orm.payload.connection`, `ORMListConnection`) used to read every matching row, with its eager loads, and slice the page in Python. It now sends `LIMIT first + 1` and `OFFSET`, on all three backends, sync and async. `totalCount` is counted only when selected, and not when the page came back short. `last` counts to locate its window instead of reading the table. Pages, cursors and `pageInfo` are unchanged. See [Connection fields](README.md#connection-fields) for what is still read whole.
- `totalCount` selected with `aggregates` or `groups` raised `'coroutine' object has no attribute 'total_count'` on async backends (Tortoise, SQLAlchemy `AsyncSession`, async Django).
- Tortoise: `groups { items }` failed with an SQL syntax error, because the items were fetched before the group key fields were known.
- `count_query` works under async execution on Django and with an `AsyncSession`, counts `DISTINCT` queries correctly on Tortoise, and on SQLAlchemy counts an entity a join repeats once, as the rows are read.
- SQLAlchemy: a refused or dropped connection is reported as `ConnectionError("Database connection failed")` instead of `Invalid filter expression`. Both messages still hide the database error from clients, which names tables, columns or the host; it is kept as the exception's cause, for the server log.

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
