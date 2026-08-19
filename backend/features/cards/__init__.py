"""Cards slice — TavernCard V3/V2/V1 parsing, remote download, profile drafting.

``parsing`` is the pure card (de)serialization logic; ``downloader`` wraps it
with remote-source browse/randomize/download (``downloader`` imports ``parsing``).
``public_profile`` is the one drafter behind both public-profile generate routes;
``sheet_update`` is its sibling for the scene-local sheet a member reads about
itself, proposing a rewrite from a finished beat and applying nothing.
"""

from __future__ import annotations

from .downloader import browse, download_card, randomize
from .parsing import card_to_dict, from_json_obj, parse, read_orb_id, to_png
from .public_profile import (
    MAX_FIELD_WORDS,
    PROFILE_FLOOR,
    ProfileDraftUnavailable,
    PublicProfileDraft,
    draft_card_profile,
    draft_scene_profile,
)
from .sheet_update import (
    SHEET_FLOOR,
    SheetUpdate,
    SheetUpdateUnavailable,
    build_beat_transcript,
    propose_sheet_update,
)

__all__ = [
    # parsing
    "card_to_dict",
    "from_json_obj",
    "parse",
    "read_orb_id",
    "to_png",
    # downloader
    "browse",
    "download_card",
    "randomize",
    # public_profile
    "MAX_FIELD_WORDS",
    "PROFILE_FLOOR",
    "ProfileDraftUnavailable",
    "PublicProfileDraft",
    "draft_card_profile",
    "draft_scene_profile",
    # sheet_update
    "SHEET_FLOOR",
    "SheetUpdate",
    "SheetUpdateUnavailable",
    "build_beat_transcript",
    "propose_sheet_update",
]
