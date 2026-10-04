"""0046_character_card_extensions -- persist the V2 card `extensions` dict.

Previously parsed on import but silently dropped. Stored as JSON so third-party extension data round-trips through export, and
Orb's own card-embedded fragments live at extensions.orb.fragments.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("character_cards", "extensions TEXT DEFAULT NULL", migration="0046")
