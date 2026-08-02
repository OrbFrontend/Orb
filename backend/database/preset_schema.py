"""Preset engine policy -- the human-decided facts the schema can't tell the engine.

The merge engine in ``backend/features/presets/engine.py`` reads the live SQLite schema and derives
every *mechanical* decision itself (merge order, id remapping, FK rewrite,
child-replace scope), so most schema changes need **no edit here**. This file holds
only the handful of facts no ``PRAGMA`` can reveal:

    which domain a table belongs to    -> DOMAIN_ROOTS
    which tables to ignore entirely     -> EXCLUDED_TABLES
    which columns are secret/personal   -> SECRET_COLUMNS  (tripwire: SENSITIVE_*)
    where a secret hides inside JSON    -> SECRET_JSON_PATHS
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
    declared columns' happy path. The same canary is derived from
    ``SECRET_JSON_PATHS`` as well, so every declared path is proved to be *walked*
    rather than merely declared.
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
    "documents": "documents",
    "settings": "configs",
    "endpoints": "configs",
    "user_personas": "configs",
}

# Touch when: you add a table the engine must never export or merge -- bookkeeping,
# caches, or migration-only artefacts. The coverage test forces the choice for every
# new table: give it a domain, or exclude it here. Current entries:
#   * orb_preset_meta      -- the preset's own descriptor row
#   * schema_migrations    -- migration bookkeeping (stamped separately)
#   * message_attachments  -- legacy, empty post-0020; gone from schema.py but still
#     present (empty) in DBs upgraded under older builds whose init_db recreated it
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
    ("endpoints", "proxy"): "",
    # Free-form and user-supplied, so its contents are unknown and may be
    # sensitive; declared secret alongside endpoints.proxy. model_configs is not
    # a singleton table, so these rows are dropped by the cascade when configs is
    # not exported rather than blanked in place. extra_body is deliberately not
    # declared -- it holds routing and tuning config worth carrying across.
    ("model_configs", "extra_headers"): "",
}

# Touch when: a workflow starts storing a credential inside one of the free-form
# JSON columns (the coverage test walks every registered workflow's normalized
# config/profile and fails here naming the missing path). Maps
# ``(table, column) -> the JSON paths whose leaf must be blanked``, with ``"*"``
# standing for "every key at this level" (a provider map keyed by provider id).
#
# A JSON column can never be name-detected -- ``is_sensitive_column("workflow_config")``
# is False and always will be -- so ``SECRET_COLUMNS`` cannot express this. Nor can
# the column simply be declared secret there: blanking ``settings.workflow_config``
# wholesale would destroy every style, imported graph and TTS setting, and a blind
# recursive blank-by-key-name would mangle the node inputs of an imported ComfyUI
# graph. Paths are the only statement narrow enough to be correct.
#
# **All three ``workflow_state`` columns are declared, two of them empty.**
# ``workflow_state`` is the same free-form per-workflow JSON slot on ``conversations``,
# ``character_cards`` and ``messages``, written through the same toolkit helpers; only
# the character one holds a credential today. Declaring all three makes this table a
# statement about every column that *could* hold one, and gives the coverage test
# something to assert completeness against -- an absent key and an empty tuple say
# different things.
SECRET_JSON_PATHS: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = {
    ("settings", "workflow_config"): (
        ("image_gen", "external_comfy", "api_key"),
        ("image_gen", "cloud", "providers", "*", "api_key"),
    ),
    ("character_cards", "workflow_state"): (("tts", "api_key"),),
    ("conversations", "workflow_state"): (),
    ("messages", "workflow_state"): (),
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
    "settings": (
        "attachment_cache_budget_bytes",
        "attachment_access_counter",
        "generated_chars",
        "workflows_globally_enabled",
        "workflow_enabled",
        "local_ml_enabled",
    ),
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
