"""Prepare an uploaded voice clip for reference cloning: vocal separation and level."""

from __future__ import annotations

from .catalog import FEATURE, ready
from .prepare import PreparedClip, prepare
from .separator import release

__all__ = ["FEATURE", "PreparedClip", "prepare", "ready", "release"]
