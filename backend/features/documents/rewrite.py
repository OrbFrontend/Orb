"""Shape a Document-mode selection for the local prose rewriter."""

from __future__ import annotations

from collections.abc import Callable

from .audit import is_scaffold_line


def split_margins(text: str) -> tuple[str, str, str]:
    """``(lead, core, trail)`` with the surrounding whitespace the rewriter strips, so a rewrite splices back in place."""
    core = text.strip()
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip())]
    return lead, core, text[len(lead) + len(core) :]


def scaffold_keeper(assisted: bool) -> Callable[[str], bool]:
    """The pass-through test for the document's mode, so prompt scaffolding is never rewritten."""
    return lambda paragraph: is_scaffold_line(paragraph, assisted)
