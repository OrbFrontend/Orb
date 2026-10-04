"""Define preset coverage and secret-column policy."""

from __future__ import annotations

# Map new top-level entities to domains; children inherit through ownership edges.
# Domain names are stable export identifiers. Roots have no incoming CASCADE edge.
OWNERSHIP_COLUMNS: dict[str, str] = {"conversation_worlds": "conversation_id"}

DOMAIN_ROOTS: dict[str, str] = {
    "conversations": "chats",
    "character_cards": "characters",
    # The Character Library's curated tag vocabulary. In "characters" rather than "configs" because the cards carry its names in
    # their own tags column: exporting cards without the vocabulary would ship tags that match no chip, leaving an empty chip
    # row and every card stale.
    "library_tags": "characters",
    "worlds": "lorebooks",
    "mood_fragments": "fragments",
    "interactive_fragments": "fragments",
    "phrase_bank": "phrase_bank",
    # Suggestion keys the user dismissed: decisions about the bank, so they
    # travel with it. The suggestions themselves are derived and excluded below.
    "slop_dismissals": "phrase_bank",
    "documents": "documents",
    "settings": "configs",
    "endpoints": "configs",
    "user_personas": "configs",
}

# Excluded tables whose rows an install derives from its own data and rebuilds on its own: the suggestion miner's output and
# bookkeeping. An export clears them, because the suggestions quote chat sentences, which must not ride along in a preset that
# leaves the chats out.
DERIVED_TABLES: frozenset[str] = frozenset({"slop_suggestions", "slop_mining_state"})

# Exclude bookkeeping, caches and historical artifacts from export/merge.
# Coverage checks require every table to have a domain or an exclusion.
EXCLUDED_TABLES: frozenset[str] = (
    frozenset({"orb_preset_meta", "schema_migrations", "message_attachments", "dataset_meta"}) | DERIVED_TABLES
)

# Touch when: a migration adds a column holding a key, the user's identity, or their prompts (the coverage test will fail and
# point you here); drop an entry only when its column leaves the schema. Map ``(table, column) -> the value to blank it to``.
# These are wiped when the ``configs`` domain is *not* exported, so a shared preset never leaks secrets. Columns on a
# non-singleton table (e.g. endpoints.api_key) are deleted with their whole row on export -- list them anyway so the coverage
# check and the generic key-strip path both see them.
SECRET_COLUMNS: dict[tuple[str, str], str] = {
    ("settings", "user_name"): "User",
    ("settings", "user_description"): "",
    ("settings", "system_prompt"): "",
    ("settings", "shared_system_prompt"): "",
    ("settings", "agent_shared_system_prompt"): "",
    ("endpoints", "api_key"): "",
    ("endpoints", "proxy"): "",
    # Free-form and user-supplied, so its contents are unknown and may be sensitive; declared secret alongside endpoints.proxy.
    # model_configs is not a singleton table, so these rows are dropped by the cascade when configs is not exported rather than
    # blanked in place. extra_body is deliberately not declared -- it holds routing and tuning config worth carrying across.
    ("model_configs", "extra_headers"): "",
}

# Credential leaf paths by (table, column); "*" matches every key at a level. Use explicit paths: whole-column or recursive
# key-name scrubbing would erase valid workflow settings and imported graph inputs. Declare all four workflow_state columns,
# even when empty, so coverage checks distinguish reviewed columns from missing declarations.
SECRET_JSON_PATHS: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = {
    ("settings", "workflow_config"): (
        ("image_gen", "external_comfy", "api_key"),
        ("image_gen", "cloud", "providers", "*", "api_key"),
    ),
    ("character_cards", "workflow_state"): (("tts", "api_key"),),
    # Card-site logins, keyed by source; the account name beside each token is not a credential.
    ("settings", "card_source_auth"): (("*", "token"),),
    ("conversations", "workflow_state"): (),
    ("messages", "workflow_state"): (),
    ("group_members", "workflow_state"): (),
}

# Touch when: exporting one domain only makes sense alongside another (a product rule, not a schema fact). Maps a domain to the
# domains dragged in with it. Today: chats are meaningless without their character cards.
IMPLIED_DOMAINS: dict[str, frozenset[str]] = {"chats": frozenset({"characters"})}

# Touch when: a singleton table (overwritten in place on import, like ``settings``) gains a column describing *local machine
# state* the import must keep rather than take from the file -- e.g. attachment-cache bookkeeping, not user-facing config. Maps
# ``table -> columns to leave untouched`` during the overwrite.
PRESERVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": (
        "attachment_cache_budget_bytes",
        "attachment_access_counter",
        "generated_chars",
        "workflows_globally_enabled",
        "workflow_enabled",
        "local_ml_enabled",
        "local_ml_config",
        # A card-site login belongs to this machine; an imported preset never signs it out or into someone else's account.
        "card_source_auth",
    )
}

# Leaf names an export with ``strip_keys`` blanks wherever a declared secret ends in one: plain SECRET_COLUMNS by column name,
# SECRET_JSON_PATHS by the path's last key. Other declared secrets (prompts, the user's name) are config worth sharing.
CREDENTIAL_LEAVES: frozenset[str] = frozenset({"api_key", "token"})

# Sensitive-name suffixes trip coverage checks unless declared in SECRET_COLUMNS. Add patterns for missed secrets; handle false
# positives by declaration, not by narrowing detection. Suffix matching avoids max_tokens/top_k false positives.
SENSITIVE_SUFFIXES: tuple[str, ...] = ("_key", "password", "token")
SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("secret",)

# Immutable identity keys are not credentials. Keep this explicit so the
# secret-name tripwire can stay broad without blanking group attribution.
NON_SECRET_KEY_COLUMNS: frozenset[tuple[str, str]] = frozenset({("group_members", "speaker_key")})


def is_sensitive_column(name: str) -> bool:
    c = name.lower()
    return c.endswith(SENSITIVE_SUFFIXES) or any(s in c for s in SENSITIVE_SUBSTRINGS)
