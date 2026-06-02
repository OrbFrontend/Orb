"""Domain data contracts owned by the database (the model layer).

These describe the *shape* of persisted data and depend on nothing else in the
codebase, so every other layer can point its dependencies inward, toward the
data — never the reverse. Anything in backend/database/ that reaches "up" into
passes/ or the orchestrator for a shared shape is an architectural inversion;
put the shape here instead.
"""

from __future__ import annotations

from typing import Union

# A phrase-bank group is either a legacy list of literal variant strings, or a
# {"kind": "literal"|"regex", ...} dict. The matching semantics that consume
# this shape live in backend/passes/editor/slop_detector.py.
PhraseGroup = Union[list[str], dict]
