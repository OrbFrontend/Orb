"""Public API for the Phrase Bank's learned slop suggestions.

Model replies across every chat are compared with card-authored text, and the phrases and sentence shapes the model overuses are
offered as ready-to-save regexes. A run starts only when the user asks for one, and nothing reaches the bank until the user
accepts a suggestion.
"""

from __future__ import annotations

from .runner import refresh, refreshing, shutdown
