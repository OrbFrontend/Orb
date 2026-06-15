"""Summarization slice — narrative summary + compress flow.

A user feature (not a pipeline pass); depends only downward on ``inference`` +
``core``.
"""

from __future__ import annotations

from .summarizer import ConversationSummarizer

__all__ = ["ConversationSummarizer"]
