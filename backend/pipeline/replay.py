"""What a downstream agent-lane call replays to reach the Writer's KV cache.

Its own module rather than a member of ``state.py`` for an import-order reason
that is worth stating: ``state`` imports the editor package (for
``LengthGuard``), and the editor, feedback, and direction-note steps all need
this value. Putting it in ``state`` makes those three import a partially
initialized module. It depends on nothing but ``core``, so it sits below all of
them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import WireMessage

CANONICAL_DRAFT_BLOCK = (
    "[OOC: Your complete reply this turn is the following text, which spans the assistant messages above. "
    "Treat it as one continuous draft.\n\n{draft}\n]\n\n"
)
"""Host-authored, and prepended to a downstream request that replays a tool
transcript.

Duplicating the draft costs tokens. The alternative -- wording that asks the
model to concatenate the assistant messages around the tool messages itself --
is exactly the ambiguity an exact-match patch tool cannot absorb, and the
Editor's whole job is exact matching. Pay the tokens.
"""


@dataclass(frozen=True, slots=True)
class WriterReplay:
    """The messages a downstream agent-lane call sends after the shared prefix.

    Two shapes, chosen by topology rather than by convenience:

    * the **normalized** ``user(writer request) + assistant(draft)`` pair, which
      is what every turn produced before Writer tools and what a dual-model
      agent lane must still receive -- its base does not declare the Writer's
      tool, so replaying a call to that tool would be a transcript the agent
      model cannot account for, and there is no Writer-lane cache on that
      server to extend anyway;
    * the **sanitized trace** in single-model mode after a tool call, which
      extends the exact bytes the Writer just used.

    ``canonical_draft_block`` is non-empty only for the second shape. The
    assistant message immediately before a downstream request may then contain
    only the post-tool continuation, so the request has to say which text is
    "the draft" instead of leaving the model to infer it from a transcript that
    spans several assistant/tool messages.
    """

    messages: tuple[WireMessage, ...]
    canonical_draft_block: str = ""

    @classmethod
    def normalized(cls, writer_user_msg, draft: str) -> WriterReplay:
        """The pre-Writer-tool shape: one user request, one assistant draft."""
        return cls(
            messages=(
                {"role": "user", "content": writer_user_msg},
                {"role": "assistant", "content": draft},
            )
        )

    def with_draft(self, writer_user_msg, draft: str) -> WriterReplay:
        """Return this replay shape updated to name *draft* authoritatively.

        A normalized replay carries the draft in its assistant message, while a
        Writer-tool trace must retain the exact warmed transcript and update only
        the host-authored canonical block appended to the downstream request.
        """
        if self.canonical_draft_block:
            return WriterReplay(
                messages=self.messages,
                canonical_draft_block=CANONICAL_DRAFT_BLOCK.format(draft=draft),
            )
        return WriterReplay.normalized(writer_user_msg, draft)


__all__ = ["CANONICAL_DRAFT_BLOCK", "WriterReplay"]
