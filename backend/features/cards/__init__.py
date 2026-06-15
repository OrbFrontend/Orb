"""Cards slice — TavernCard V2 parsing + remote card download.

``parsing`` is the pure card (de)serialization logic; ``downloader`` wraps it
with remote-source browse/randomize/download (``downloader`` imports ``parsing``).
"""

from __future__ import annotations

from .downloader import browse, download_card, randomize
from .parsing import card_to_dict, from_json_obj, parse, read_orb_id, to_png

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
]
