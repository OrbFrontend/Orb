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
