"""The assistant's conversation, held by the server between requests so each one extends what the model already read."""

from __future__ import annotations

from dataclasses import replace

from .contracts import RequestMark, Transcript, is_request_message

# One conversation, named by the id the browser made for it. Process memory, as the proposals are: a restart starts over.
_held: tuple[str, Transcript] | None = None


def held_transcript(session: str) -> Transcript | None:
    """The conversation *session* names, or ``None`` when the server holds another one or none."""
    return _held[1] if _held is not None and _held[0] == session else None


def hold_transcript(session: str, transcript: Transcript) -> None:
    """Keep *transcript* as *session*'s conversation, in place of whichever was held."""
    global _held
    _held = (session, transcript)


def rewound(transcript: Transcript, request_id: str) -> tuple[Transcript, tuple[RequestMark, ...]]:
    """*transcript* as it stood before the request *request_id*, and the marks of that request and every one after it.

    This is how an edited request is taken: the conversation goes back to where the request began and the new wording
    extends it from there. Nothing else goes back: what the removed requests changed stays changed. A request the
    conversation no longer holds leaves nothing to extend, so the edit starts a conversation of its own.
    """
    starts = [index for index, message in enumerate(transcript.messages) if is_request_message(message)]
    marks = transcript.requests
    from_end = next((len(marks) - at for at, mark in enumerate(marks) if mark.id == request_id), 0)
    if not 0 < from_end <= len(starts):
        return replace(transcript, messages=(), requests=()), ()
    kept = len(marks) - from_end
    return replace(transcript, messages=transcript.messages[: starts[-from_end]], requests=marks[:kept]), marks[kept:]


def rewind_note(removed: tuple[RequestMark, ...]) -> str:
    """What an edited request is told of the requests it replaced: only the changes they committed, which the model can no
    longer see being made. Empty when they changed nothing."""
    committed = [line for mark in removed for line in mark.committed]
    if not committed:
        return ""
    return (
        "[The user rewrote this request. The requests that followed its earlier wording were removed from this "
        "conversation, but what they changed was not undone and still stands:\n"
        + "\n".join(f"- {line}" for line in committed)
        + "]"
    )
