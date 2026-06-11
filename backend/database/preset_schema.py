"""Product / security policy for the preset engine -- the single source of truth.

The merge engine in ``backend/presets.py`` derives *all* of its mechanics (merge
order, id remapping, FK rewrite, child-replace scope) from the live schema via
``PRAGMA`` introspection, so adding a child table or a new FK column needs **zero**
edits there. What is *not* a schema fact -- which root table belongs to which
user-facing domain, which machinery tables to ignore, which columns carry secrets --
lives here and only here. ``tests/integration/test_preset_schema_coverage.py`` fails
loudly the moment a freshly-migrated table or a sensitive-looking column is not
accounted for below.
"""

from __future__ import annotations

# Root table -> user-facing domain. A *root* owns no other table (it has no
# ``ON DELETE CASCADE`` foreign key pointing at a parent). Every non-root table
# auto-joins its root's domain by following ownership edges upward, so only the
# roots need listing here. This is the schema-driven replacement for the old
# hand-maintained DOMAIN_TABLES map.
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

# Machinery / legacy tables the engine neither exports nor merges:
#   * orb_preset_meta   -- the preset's own descriptor row
#   * schema_migrations -- migration bookkeeping (stamped separately)
#   * message_attachments -- always empty post-0020 (migration moves its rows to
#     user_attachments and the table is retained only as a fresh-install artefact)
EXCLUDED_TABLES: frozenset[str] = frozenset({"orb_preset_meta", "schema_migrations", "message_attachments"})

# Secret / personal columns blanked when the ``configs`` domain is *not* exported,
# so a shared preset never leaks an API key, the user's identity, or their prompts.
# Maps ``(table, column) -> replacement value``. This is a security decision, not a
# schema fact, so it is declared rather than derived. Entries on a non-singleton
# table (e.g. endpoints.api_key) are moot for the export scrub -- those rows are
# deleted wholesale -- but are listed so the coverage check sees every key column
# accounted for, and so the key-stripping export path can find them generically.
SECRET_COLUMNS: dict[tuple[str, str], str] = {
    ("settings", "api_key"): "",
    ("settings", "user_name"): "User",
    ("settings", "user_description"): "",
    ("settings", "system_prompt"): "",
    ("settings", "shared_system_prompt"): "",
    ("settings", "agent_shared_system_prompt"): "",
    ("endpoints", "api_key"): "",
}

# Exporting one domain implies exporting another (a product rule, not a schema
# fact): chats are meaningless without their character cards.
IMPLIED_DOMAINS: dict[str, frozenset[str]] = {
    "chats": frozenset({"characters"}),
}

# Columns carried across the settings-singleton overwrite on import rather than
# taken from the file: they describe local ``workflow_attachments`` rows that an
# import retains, not a user-facing config (see bootstrap.reset_to_defaults).
PRESERVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": ("attachment_cache_budget_bytes", "attachment_access_counter"),
}

# Markers that flag a column as security-sensitive. The coverage check fails on any
# matching column not present in SECRET_COLUMNS, so a newly added secret cannot slip
# into a shared preset unnoticed. Matched as *suffixes* (plus the "secret" substring)
# rather than loose substrings, so ``api_key`` / ``auth_token`` are caught while
# innocuous names like ``max_tokens`` or ``top_k`` are not.
SENSITIVE_SUFFIXES: tuple[str, ...] = ("_key", "password", "token")
SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("secret",)


def is_sensitive_column(name: str) -> bool:
    c = name.lower()
    return c.endswith(SENSITIVE_SUFFIXES) or any(s in c for s in SENSITIVE_SUBSTRINGS)
