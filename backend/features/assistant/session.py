"""The assistant's conversation, held by the server between requests so each one extends what the model already read."""

from __future__ import annotations

from .contracts import Transcript

# One conversation, named by the id the browser made for it. Process memory, as the proposals are: a restart starts over.
_held: tuple[str, Transcript] | None = None


def held_transcript(session: str) -> Transcript | None:
    """The conversation *session* names, or ``None`` when the server holds another one or none."""
    return _held[1] if _held is not None and _held[0] == session else None


def hold_transcript(session: str, transcript: Transcript) -> None:
    """Keep *transcript* as *session*'s conversation, in place of whichever was held."""
    global _held
    _held = (session, transcript)
