"""Global presets: named generation configs, each with its own prompt layout, switched from the sidebar header."""

from __future__ import annotations

from ...database import create_preset, rename_preset
from .engine import delete_preset, switch_preset
