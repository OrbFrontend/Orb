"""Find character heads in an avatar image."""

from __future__ import annotations

from .catalog import FEATURE, ready
from .detect import Head, heads

__all__ = ["FEATURE", "Head", "heads", "ready"]
