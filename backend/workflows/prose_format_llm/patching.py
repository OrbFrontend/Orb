"""Pure search/replace patch application.

Deliberately a separate copy of the editor's ``apply_patches`` rather than a
shared import: the editor's lives in ``backend/pipeline/``, a higher layer this
workflow may not import. It also omits the editor's quote/asterisk
normalization fallbacks on purpose -- among other things this workflow fixes
quote/asterisk markup, so normalizing during the match would hide the very
markup drift it must catch.
Keep both facts in mind before "DRY-ing" this away.
"""

from __future__ import annotations

from typing import Any


def apply_patches(draft: str, patches: Any) -> tuple[str, list[str]]:
    """Apply each ``{search, replace}`` patch to *draft*, returning the new text
    and a list of human-readable error strings for patches that were skipped.

    A patch applies only on an exact, unique match; zero or multiple matches are
    errors (the model must quote more context). Errors are never fatal -- the
    surviving patches still apply and the caller surfaces the skips.
    """
    errors: list[str] = []
    if not isinstance(patches, list):
        return draft, errors
    for i, p in enumerate(patches):
        if not isinstance(p, dict):
            errors.append(f"patch {i}: not an object")
            continue
        search = p.get("search")
        replace = p.get("replace")
        if not isinstance(search, str) or not isinstance(replace, str):
            errors.append(f"patch {i}: search/replace must be strings")
            continue
        if not search or search == replace:
            continue
        count = draft.count(search)
        if count == 0:
            errors.append(f"patch {i}: search not found")
            continue
        if count > 1:
            errors.append(f"patch {i}: search matched {count} times (not unique)")
            continue
        draft = draft.replace(search, replace, 1)
    return draft, errors
