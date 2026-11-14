# Changelog

## 0.27.0

### Added

- **Group by related rows.** `groupBy: [{object: {author: {field: {name: true}}}}]` groups by any column of a related row, through a foreign key, a reverse foreign key or a many-to-many relation. The key nests one level per relation (`key { author { id name } }`). A relation is offered when its model has a group-by type and the type shows the relation as stored; `@orm.group_type` classes name relations with `auto`. The related row is read through its type's `scope_rows`, so a hidden one groups as `null`. Across a to-many relation a row is grouped once under each distinct key its visible related rows give, and once under `null` if it has none.
- **`@group_field` works.** Its value is grouped by in the database on all three backends, and is reported as a string under the method's name in the key. It may return an expression, a field name, an `(expression, label)` tuple or, on Tortoise, a pypika term.

### Changed

- **Groups follow the connection's order.** Each group is ranked by its first row under `order` (or the query's own ordering), then by its key, so a group's edges sit together. `items` within a group follow the same order.
- **`DateGroupByOption.interval` is honoured on every backend.** Django truncates in the current time zone. SQLAlchemy and Tortoise truncate on PostgreSQL and SQLite and raise on other databases. `batch_group_items` partitions by the same expression.
  - **Breaking:** an interval key is now the ISO 8601 start of the period (`"2026-03-01T00:00:00"`, with its offset on Django under `USE_TZ`). SQLAlchemy used to key by `strftime` text such as `"2026-03"` (SQLite only), and Django and Tortoise grouped by the raw value.
- **Breaking for custom backends:** `apply_grouping` and `batch_group_items` take a list of `strawberry_orm.grouping.GroupPath` instead of the group-by input or key field names, and backends implement `relation_hop` and `group_memberships`. `scope_query_to_group`, `AggregateMeta.group_key_fields` and `AggregateMeta.build_group_key` are removed.

### Fixed

- **Grouping without `order` gave one group per row** on Django, because the query's default ordering leaked into `GROUP BY`. It no longer does on any backend.
- **A `null` group's `edgeIndices` claimed every edge.** A `null` key now matches only rows whose value is null. Related, interval and `@group_field` keys are matched by asking the database for the page's rows.
- **A filter through a to-many relation repeated rows.** On Django and Tortoise, `filter: {object: {posts: ...}}` listed and counted a parent once per matching related row. The match is now a subquery, so each row appears once in edges, `totalCount`, aggregates and groups.
- **Async Django connections returned `null` aggregates and groups** when the resolver ran through `async_safe_resolver`. The query was stashed for aggregation before the worker thread had returned it.
- **A nested connection's aggregates and groups ignored its parent on Tortoise.** Every parent's connection shared one slot on the context, and concurrent async resolvers overwrote each other's query. The state is now kept per field path.

### Known issues

- On Tortoise, a `NULLS_FIRST`/`NULLS_LAST` order is not applied to a connection's edges, as before. Groups and their `items` do follow it, so `edgeIndices` may not be contiguous under such an order.

### Changed

- **Breaking: `contrib.mcp` serves your operations as tools, with no model on the server.** `mcp_server(schema, operations=DOCUMENT)` turns each named operation in a GraphQL document into one MCP tool. The operation's name and description become the tool's, its variables become typed arguments (a JSON Schema with your input types, enums, defaults and descriptions), and its selection set becomes the tool's output schema. Queries are marked read-only and mutations destructive. The document is validated when the server is built. `QueryAgent`, the `ask` tool, clarifications, confirmations, lookups and the hooks are removed, and the extra no longer installs any-llm.
- `sync=True` runs `schema.execute_sync` in a worker thread, `sync=False` always awaits `schema.execute`, and `execute=` is an async `(query, variables, context, allowed) -> ExecutionResult` of your own. By default a query that Django refuses on the event loop is rerun with `execute_sync`, with a warning; a mutation fails asking for `sync=True` rather than run twice.

### Added

- **`graphql_tool=`** adds a `graphql` tool that runs one operation the client writes, and a `graphql_schema` tool that returns the schema it is written against. `GraphQLTool(allow_mutations=, root_fields=, max_depth=, descriptions=)` configures them; only queries run by default.
- A weekly CI run against the newest strawberry-graphql release.

### Fixed

- Sync execution closes Django's stale connections before and after each operation, as Django's request handler does. Executor threads used to keep their connection past `CONN_MAX_AGE` and never noticed one the database had dropped.

## 0.25.1

### Changed

- Requires **strawberry-graphql ≥ 0.341.2**. Connection pages still follow Strawberry's `SliceMetadata`: `first` with `before` takes the first *n* rows that precede the cursor (from the start, or after `after`), not the *n* rows immediately before it.

### Fixed

- **Columns declared on an interface count as shown.** `_orm_reads_as_is` only looked at the type's own `__annotations__`, so a field inherited from a `strawberry.interface` never opened its column to the type's filters, orders, groups or aggregates. An order whose only field was inherited failed with `InputCutError`; every other input quietly dropped those columns. Inherited fields that read as stored are now counted the same way as own annotations. A type's `exclude=` still cuts them from the inputs.
- **`ORMListConnection` is a decorated Strawberry type.** strawberry-graphql 0.334+ refuses undecorated subclasses of Strawberry types; the README's `posts: ORMListConnection[PostType] = orm.connection.eager()` pattern then failed schema build. `ORMListConnection` is now `@strawberry.type`, like `ORMConnection`.
  - **Breaking:** this renames the published type. Strawberry used to publish the undecorated subclass under its parent's name, so `ORMListConnection[PostType]` appeared as `PostTypeORMConnection`; it is now `PostTypeORMListConnection`. Every operation still validates, but anything that names the type breaks without an error: `__typename` checks, fragments `on PostTypeORMConnection`, persisted queries, client cache `possibleTypes`/`typePolicies`, and test mocks. Search clients for `ORMConnection` and rename it to `ORMListConnection`.
- **`QueryAgent` rejects a non-object `variables` value.** `variables: []` was treated as `{}` because empty lists are falsy, so a malformed tool call could run. It is now refused like any other non-object.
- **`QueryAgent` runs sync schemas.** If `await schema.execute` raises Django's `SynchronousOnlyOperation`, or Strawberry returns it as a GraphQL error's `original_error`, the agent switches to `schema.execute_sync` in a worker thread, logs a warning, and keeps that path for later calls on the same agent.

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
