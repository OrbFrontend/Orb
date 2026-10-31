"""Feature-specific prompt and output handling for local autocomplete."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ...inference import local_ml

# Machinery cards append to every reply: HTML comments, <style>/<script> blocks, fenced status blocks (```Position: ...```),
# and tags such as <img>/<audio>. They usually sit at the END of a message, which is the tail the prompt keeps, so a
# ~600-char trailer can leave the model no story text at all. An unclosed comment or fence runs to the end of the message.
_MARKUP_BLOCK_RE = re.compile(
    r"<!--.*?(?:-->|\Z)|<(style|script)\b.*?(?:</\1\s*>|\Z)|```.*?(?:```|\Z)", re.DOTALL | re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")


def strip_markup(text: str) -> str:
    """Remove non-prose markup blocks and HTML tags (keeping text between tags) and tidy the leftover blank lines."""
    text = _HTML_TAG_RE.sub("", _MARKUP_BLOCK_RE.sub("", text))
    return re.sub(r"\n[ \t\r]*(?:\n[ \t\r]*)+", "\n\n", text).strip()


async def complete(prompt: str, n_predict: int = 12, stop: Sequence[str] = ("\n",), temperature: float = 0.25) -> str:
    """Autocomplete continuation over the generic local-model runtime.

    The typeahead model produces garbage for a whitespace-ending prompt, so trim that tail before inference. If whitespace was
    removed, also remove the model's re-emitted leading separator because the frontend appends the result to the original,
    untrimmed draft.
    """
    trimmed = prompt.rstrip()
    completion = await local_ml.acomplete("autocomplete", trimmed, n_predict, stop, temperature)
    return completion.lstrip() if trimmed != prompt else completion


def build_prompt(
    char_name: str,
    user_name: str,
    char_summary: str,
    recent: Sequence[Mapping[str, str]],
    draft: str,
    *,
    max_msg_chars: int = 500,
    max_summary_chars: int = 400,
) -> str:
    """Assemble a short raw-continuation prompt ending at the user's draft.

    *recent* is oldest-to-newest and may carry a ``name`` that labels a group speaker instead of using *char_name*. The
    lightweight prompt deliberately excludes Director and pipeline injection content.
    """
    lines: list[str] = []
    summary = strip_markup(char_summary or "")
    if summary:
        lines.append(summary[:max_summary_chars])
        lines.append("***Roleplay chat below***")
    for message in recent:
        name = (message.get("name") or "").strip() or (user_name if message.get("role") == "user" else char_name)
        content = strip_markup(message.get("content") or "")[-max_msg_chars:]
        if content:
            lines.append(f"{name}: {content}")
    lines.append(f"{user_name}: {draft}")
    return "\n".join(lines)
