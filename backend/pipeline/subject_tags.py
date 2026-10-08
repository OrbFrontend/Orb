"""Subject tags for assistant replies: tagged when saved, read back per active branch, filled lazily when missing or stale."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from .. import database as db
from ..analysis.audit import audit_on
from ..analysis.subjects import SUBJECTS_INPUT_VERSION, content_hash, subjects_input
from ..core.settings import Settings
from ..inference import local_ml
from .predicates import agent_enabled

logger = logging.getLogger(__name__)

FEATURE = "subjects_classifier"

Probs = dict[str, list[float]]


class TaggedReply(NamedTuple):
    text: str
    probs: Probs


_MEMO_SIZE = 64
_memo: dict[str, Probs] = {}


def subjects_enabled(settings: Settings) -> bool:
    """The Output Auditor runs with this toggle on, a Judge model is set, and the tagger is installed, downloaded, and enabled
    in Local ML."""
    return (
        agent_enabled(settings)
        and bool(settings.get("decision_endpoint_id") and settings.get("decision_model"))
        and bool((settings.get("enabled_tools") or {}).get("editor_apply_patch"))
        and audit_on(settings.get("editor_audit_toggles"), "subject_fixation")
        and settings.get("local_ml_enabled", {}).get(FEATURE, True) is not False
        and local_ml.available(FEATURE)[0]
    )


def tag_version() -> str:
    spec = local_ml.MODELS[FEATURE]
    return f"{spec.local_name}@{spec.revision}|{SUBJECTS_INPUT_VERSION}"


async def tag_text(text: str) -> Probs:
    """One reply's tags, ``{category: [absent, action, description]}``. Repeats of a recent text reuse its reading."""
    key = content_hash(text)
    if (probs := _memo.get(key)) is None:
        if len(_memo) >= _MEMO_SIZE:
            del _memo[next(iter(_memo))]
        probs = _memo[key] = await local_ml.aclassify_subjects(subjects_input(text))
    return probs


async def _store(message_id: int, text: str, probs: Probs) -> None:
    try:
        await db.set_message_subjects(message_id, content_hash(text), tag_version(), probs)
    except sqlite3.IntegrityError:
        logger.info("Subject tags for message %s not stored: the message is gone", message_id)


async def tag_saved_reply(message_id: int, text: str, settings: Settings) -> None:
    """Tag a just-saved assistant reply. Never raises: a missing tag is filled lazily on the next read."""
    if not text.strip() or not subjects_enabled(settings):
        return
    try:
        await _store(message_id, text, await tag_text(text))
    except Exception:
        logger.exception("Subject tagging failed for message %s; it will be tagged on demand", message_id)


async def branch_tags(
    history: Sequence[Mapping[str, Any]], window: int, speaker_member_id: str | None = None
) -> list[TaggedReply]:
    """The last *window* assistant replies in *history* with their tags, newest first; in a group, only *speaker_member_id*'s.

    A reply with no stored tag, or one stored for other text or another model/input version, is tagged now and stored.
    The caller checks ``subjects_enabled`` first.
    """
    replies = [
        m
        for m in reversed(history)
        if m.get("role") == "assistant"
        and isinstance(m.get("id"), int)
        and (speaker_member_id is None or m.get("speaker_member_id") == speaker_member_id)
    ][:window]
    stored = await db.get_message_subjects([m["id"] for m in replies])
    version = tag_version()
    out: list[TaggedReply] = []
    for msg in replies:
        text = str(msg.get("content") or "")
        row = stored.get(msg["id"])
        if row is not None and row["version"] == version and row["content_hash"] == content_hash(text):
            out.append(TaggedReply(text, row["probs"]))
            continue
        probs = await tag_text(text)
        await _store(msg["id"], text, probs)
        out.append(TaggedReply(text, probs))
    return out
