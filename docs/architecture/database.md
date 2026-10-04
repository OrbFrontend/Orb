# Database initialization and upgrades

`backend/database/bootstrap.py:init_db` owns the database startup decision. The
API lifespan calls it before database maintenance and before opening the WAL
anchor. It returns the number of migrations applied, which startup uses to
reclaim pages left behind by table rebuilds.

A database with no non-internal SQLite schema objects is fresh. This includes a
missing file, a zero-byte file, and an empty SQLite database that already has a
header or internal tables. File size, empty application tables, missing settings,
and a missing migration ledger are not sufficient evidence of a fresh install.
Any existing application schema takes the upgrade path.

Fresh initialization creates the current schema from `schema.py`, inserts the
defaults from `seeds.py` through the bootstrap helpers, and records the current
migration IDs in `schema_migrations`. All three commit in one transaction. It
does not call the migration runner or import any numbered migration module. If
initialization fails, its schema, seeds, and baseline roll back together, so the
next start can retry fresh initialization. The baseline prevents historical
migrations from replaying on subsequent starts while allowing newly added
migrations to run once.

Existing databases run pending migrations before applying the current schema
script and seeding empty tables. This order matters: `CREATE TABLE IF NOT EXISTS`
does not add missing columns, and current indexes may reference columns added
by a pending migration. Existing databases must never be stamped past pending
migrations, even when they have no settings rows or migration ledger. Backup and
preset upgrades continue to use the migration runner against existing data.

Every schema or default-data change in a migration must also be represented in
the current schema or bootstrap seeds. The regression gates cover both directions:

- `test_fresh_install_stamping.py` compares fresh schema and seeds with a deliberate
  full migration replay, rejects migration execution during fresh startup, and
  covers retries, restarts, and future upgrades.
- `test_migration_chain_completeness.py` runs migrations over a frozen historical
  schema without the current schema script filling gaps, and verifies startup
  upgrades existing databases with and without user data.

Keep the historical fixture frozen. Updating it to match the current schema would
hide missing migrations for existing installations.

## Changing the schema

A schema change touches these together:

| File | Change |
|---|---|
| `backend/database/migrations/NNNN_description.py` | The next free number, with a `migrate(conn)` that takes a plain `sqlite3.Connection`. Make it safe to rerun: it is recorded as applied only after it returns. A migration that only adds columns can be `migrate = column_migration(table, *declarations)` from `migrations/helpers.py` |
| `backend/database/schema.py` | The same table or column, so a fresh install matches an upgraded one |
| `backend/database/seeds.py` | Any default rows the migration inserts |
| `backend/database/models.py` | The row `TypedDict` the queries return |
| `backend/database/preset_schema.py` | A domain or exclusion for a new table, and any column that holds a secret |
| `backend/api/schemas.py` | Request and response models, where the column is exposed |

The runner picks up every `NNNN_*.py` file in the migrations directory, so a
migration needs no registration. `test_preset_schema_coverage.py` fails until a
new table or secret column is classified, and the two gates above fail when the
migration and the current schema disagree.

`run_unix.sh` and `run_windows.bat` start uvicorn with `--reload` on `backend/`,
so a new migration file runs against your real database, and is recorded as
applied, as soon as it is saved. Stop the server while writing one.

## Writing queries

SQL lives in `backend/database/queries/`, and each module's public functions are
re-exported from `backend/database/__init__.py`. Routes, features, and the
pipeline call those functions rather than opening a connection themselves.
`backend/database/connection.py` has one helper for each kind of access:

| Helper | Use it for |
|---|---|
| `select_rows(sql, params)` | A single read. The connection closes before the query decodes the rows. |
| `get_db()` | Several statements that need one connection, such as a read loop or a write that calls `commit()` itself. |
| `immediate_tx()` | Read-modify-write. It holds `BEGIN IMMEDIATE` for the whole block, commits on success, and rolls back on any exception, including cancellation. |

Decode JSON columns in the query function and return the row `TypedDict` from
`models.py`, so callers never see raw column text.

## Concurrent saves and dataset replacement

Document content writes require `expected_revision`. The database compares and
increments `documents.revision` in one conditional update and reads the response
inside the same transaction. A mismatch returns 409 with the current document.
Title-only writes preserve content and generated spans.

`dataset_meta` stores a persistent dataset epoch, excluded from preset merging.
Admission tracks mutating HTTP requests through response cleanup, alongside detached
workflow jobs and registered streams. Apply/restore closes mutation admission and
refuses with a list of running work before modifying the dataset; export/download
remain available. Successful apply/restore regenerates the epoch. Browser API and
raw SSE/keepalive writes carry `X-Orb-Epoch`; a stale value returns 409 with
`refresh_required`, prompting draft preservation and a reload.

Local model deletion also holds the download lock. Managed llama hosts retain their
admission lock from active-use drain through unlink; ONNX inference holds a lease,
and exclusive release drains leases before file mutation. A drain timeout returns
409 and keeps the file.

Card deletion checks referencing chats inside its write transaction. Duplicate
resolution validates every card and affected chat, then relinks and removes the
whole cluster in one transaction. Chat admission stays fenced through commit;
any refusal rolls back all members of the choice.
