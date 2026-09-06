"""Feature-specific prompt and output handling for local autocomplete."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...inference import local_ml


async def complete(
    prompt: str,
    n_predict: int = 12,
    stop: Sequence[str] = ("\n",),
    temperature: float = 0.25,
) -> str:
    """Autocomplete continuation over the generic local-model runtime.

    The typeahead model produces garbage for a whitespace-ending prompt, so
    trim that tail before inference. If whitespace was removed, also remove the
    model's re-emitted leading separator because the frontend appends the result
    to the original, untrimmed draft.
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

    *recent* is oldest-to-newest and may carry a ``name`` that labels a group
    speaker instead of using *char_name*. The lightweight prompt deliberately
    excludes Director and pipeline injection content.
    """
    lines: list[str] = []
    summary = (char_summary or "").strip()
    if summary:
        lines.append(summary[:max_summary_chars])
        lines.append("***Roleplay chat below***")
    for message in recent:
        name = (message.get("name") or "").strip() or (user_name if message.get("role") == "user" else char_name)
        content = (message.get("content") or "").strip()[-max_msg_chars:]
        if content:
            lines.append(f"{name}: {content}")
    lines.append(f"{user_name}: {draft}")
    return "\n".join(lines)
