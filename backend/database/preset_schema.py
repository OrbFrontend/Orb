"""Preset engine policy -- the human-decided facts the schema can't tell the engine.

The merge engine in ``backend/presets.py`` reads the live SQLite schema and derives
every *mechanical* decision itself (merge order, id remapping, FK rewrite,
child-replace scope), so most schema changes need **no edit here**. This file holds
only the handful of facts no ``PRAGMA`` can reveal:

    which domain a table belongs to    -> DOMAIN_ROOTS
    which tables to ignore entirely     -> EXCLUDED_TABLES
    which columns are secret/personal   -> SECRET_COLUMNS  (tripwire: SENSITIVE_*)
    product rules layered on top         -> IMPLIED_DOMAINS, PRESERVED_COLUMNS

You don't have to remember when to touch them: ``tests/integration/
test_preset_schema_coverage.py`` fails the moment a migration adds a table or a
secret-looking column that isn't accounted for, and names the constant to fix. Each
section below opens with a "Touch when:" line saying exactly what to change.

Three edits that once corrupted presets *silently* -- each now has a dedicated
tripwire, so they fail loudly instead:
  * Renaming a domain value. Domains are baked into every exported file
    (``orb_preset_meta.included_domains``); a renamed domain no longer matches on
    import, so that data is silently skipped for every preset already out there.
    Add domains freely; never rename one. CAUGHT BY: a frozen-literal assertion on
    ``presets.ALL_DOMAINS`` in the coverage test -- a rename fails CI; an addition
    is a deliberate one-line test edit.
  * Parking a real data table in ``EXCLUDED_TABLES`` to quiet the test -- excluded
    tables are invisible to export *and* merge, so the data vanishes from backups.
    CAUGHT BY: a runtime tripwire in ``build_preset`` that raises if any excluded
    table other than the meta/migration bookkeeping holds rows, plus a test that
    every excluded data table is empty in the fresh schema.
  * Narrowing ``SENSITIVE_*`` to clear a flagged column -- declare the column in
    ``SECRET_COLUMNS`` instead, or the secret ships in shared presets. CAUGHT BY:
    a secret-canary test that seeds a unique sentinel into every secret column,
    exports without ``configs`` (and with ``strip_keys``), and greps the produced
    file's raw bytes for any surviving canary -- a generic leak check, not just the
    declared columns' happy path.
"""

from __future__ import annotations

# Touch when: you add a brand-new top-level entity -- map its table to a user-facing
# domain. A child table hung off an existing entity needs no entry; it inherits its
# root's domain automatically. Reuse a domain or mint a new value (a new value mints
# a new exportable domain -- ALL_DOMAINS is derived from these); never rename one
# (see header).
#
# A *root* owns no other table: nothing points at it via ``ON DELETE CASCADE``.
# Non-root tables join their root's domain by following ownership edges upward.
DOMAIN_ROOTS: dict[str, str] = {
    "conversations": "chats",
    "character_cards": "characters",
    "worlds": "lorebooks",
    "mood_fragments": "fragments",
    "interactive_fragments": "fragments",
    "phrase_bank": "phrase_bank",
    "settings": "configs",
    "endpoints": "configs",
    "user_personas": "configs",
}

# Touch when: you add a table the engine must never export or merge -- bookkeeping,
# caches, or migration-only artefacts. The coverage test forces the choice for every
# new table: give it a domain, or exclude it here. Current entries:
#   * orb_preset_meta      -- the preset's own descriptor row
#   * schema_migrations    -- migration bookkeeping (stamped separately)
#   * message_attachments  -- empty post-0020; retained only as a fresh-install artefact
EXCLUDED_TABLES: frozenset[str] = frozenset({"orb_preset_meta", "schema_migrations", "message_attachments"})

# Touch when: a migration adds a column holding a key, the user's identity, or their
# prompts (the coverage test will fail and point you here); drop an entry only when
# its column leaves the schema. Map ``(table, column) -> the value to blank it to``.
# These are wiped when the ``configs`` domain is *not* exported, so a shared preset
# never leaks secrets. Columns on a non-singleton table (e.g. endpoints.api_key) are
# deleted with their whole row on export -- list them anyway so the coverage check
# and the generic key-strip path both see them.
SECRET_COLUMNS: dict[tuple[str, str], str] = {
    ("settings", "api_key"): "",
    ("settings", "user_name"): "User",
    ("settings", "user_description"): "",
    ("settings", "system_prompt"): "",
    ("settings", "shared_system_prompt"): "",
    ("settings", "agent_shared_system_prompt"): "",
    ("endpoints", "api_key"): "",
}

# Touch when: exporting one domain only makes sense alongside another (a product
# rule, not a schema fact). Maps a domain to the domains dragged in with it. Today:
# chats are meaningless without their character cards.
IMPLIED_DOMAINS: dict[str, frozenset[str]] = {
    "chats": frozenset({"characters"}),
}

# Touch when: a singleton table (overwritten in place on import, like ``settings``)
# gains a column describing *local machine state* the import must keep rather than
# take from the file -- e.g. attachment-cache bookkeeping, not user-facing config.
# Maps ``table -> columns to leave untouched`` during the overwrite.
PRESERVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": ("attachment_cache_budget_bytes", "attachment_access_counter"),
}

# The tripwire behind the SECRET_COLUMNS check: any column whose name ends with one
# of these suffixes (or contains "secret") must appear in SECRET_COLUMNS, or the
# coverage test fails -- so a new secret can't slip into a shared preset unnoticed.
# Touch when: a real secret evades every pattern (e.g. ``credentials_blob``) -- add a
# pattern so it's caught. To clear a *false* positive, declare the column in
# SECRET_COLUMNS, never narrow these (see header). Suffix-matched (not loose
# substring) so ``api_key`` / ``auth_token`` are caught while ``max_tokens`` /
# ``top_k`` are not.
SENSITIVE_SUFFIXES: tuple[str, ...] = ("_key", "password", "token")
SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("secret",)


def is_sensitive_column(name: str) -> bool:
    c = name.lower()
    return c.endswith(SENSITIVE_SUFFIXES) or any(s in c for s in SENSITIVE_SUBSTRINGS)
