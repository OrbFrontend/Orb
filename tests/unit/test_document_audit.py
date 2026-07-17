"""Unit tests for the Document-mode Output Auditor slice
(features/documents/audit.py) and the report_to_dict serializer.

Covers the trim/clean/toggle pure helpers, the audit orchestrator's
draft-scoping + truncation semantics, and the one-shot patch flow against a
stub client (forced editor_apply_patch call, tail reattachment, patch-error
surfacing).
"""

from __future__ import annotations

import json

from backend.analysis import AuditReport, report_to_dict, run_audit
from backend.features.documents.audit import (
    DOC_AUDIT_TYPES,
    audit_document,
    build_patch_messages,
    clean_context,
    doc_audit_toggles,
    patch_document,
    trim_incomplete_tail,
)

_BANNED = "shivers down her spine"
_BANK = [[_BANNED]]  # one literal phrase group, detector-facing shape

_SETTINGS = {"temperature": 0.7, "max_tokens": 512}


# ── trim_incomplete_tail ─────────────────────────────────────────────────────


def test_trim_mid_sentence_splits_at_last_boundary():
    core, tail = trim_incomplete_tail("He ran. She sto")
    assert core == "He ran. "
    assert tail == "She sto"


def test_trim_reattachment_invariant():
    for draft in ("He ran. She sto", "One. Two! Thr", '"Stop!" she said. And he wal', "no boundary at all", ""):
        core, tail = trim_incomplete_tail(draft)
        assert core + tail == draft


def test_trim_complete_draft_untouched():
    assert trim_incomplete_tail("A complete sentence.") == ("A complete sentence.", "")
    # Trailing closing markers after the terminator still count as complete.
    assert trim_incomplete_tail('He said "stop."') == ('He said "stop."', "")
    assert trim_incomplete_tail("Emphatic ending!*") == ("Emphatic ending!*", "")
    # Trailing whitespace after a terminator is complete too.
    assert trim_incomplete_tail("Done here.\n") == ("Done here.\n", "")


def test_trim_single_partial_sentence_yields_empty_core():
    assert trim_incomplete_tail("just a fragment with no end") == ("", "just a fragment with no end")


def test_trim_empty_and_whitespace():
    assert trim_incomplete_tail("") == ("", "")
    assert trim_incomplete_tail("   \n") == ("", "   \n")


# ── clean_context ────────────────────────────────────────────────────────────


def test_clean_context_assisted_strips_macro_lines():
    ctx = "### SYSTEM: be terse\nSome prose here.\n### USER: darker now\nMore prose."
    assert clean_context(ctx, assisted=True) == "Some prose here.\nMore prose."


def test_clean_context_raw_strips_template_marker_lines():
    # Any line carrying a <|…|> token is template scaffold, not prose.
    ctx = "<|im_start|>user\nWrite a haiku.<|im_end|>\nPlain prose line."
    assert clean_context(ctx, assisted=False) == "Plain prose line."


def test_clean_context_heuristics_are_mode_scoped():
    # Raw mode leaves ### macro lines alone (they are literal prose there)…
    assert clean_context("### USER: literal\nprose", assisted=False) == "### USER: literal\nprose"
    # …and assisted mode leaves template markers alone.
    assert clean_context("<|im_start|>\nprose", assisted=True) == "<|im_start|>\nprose"


def test_clean_context_plain_prose_is_untouched_and_capped():
    prose = "Just an ordinary paragraph."
    assert clean_context(prose, assisted=True) == prose
    assert clean_context(prose, assisted=False) == prose
    long = "x" * 10000
    assert len(clean_context(long, assisted=False)) == 8000


# ── doc_audit_toggles ────────────────────────────────────────────────────────


def test_toggles_none_defaults_all_on():
    assert doc_audit_toggles(None) == {k: True for k in DOC_AUDIT_TYPES}


def test_toggles_intersected_with_doc_subset():
    stored = {"banned_phrases": False, "anti_echo": True, "structural_repetition": False}
    out = doc_audit_toggles(stored)
    assert set(out) == set(DOC_AUDIT_TYPES)  # chat-only keys never pass through
    assert out["banned_phrases"] is False
    assert out["repetitive_openers"] is True  # missing key defaults on


# ── report_to_dict ───────────────────────────────────────────────────────────


def test_report_to_dict_clean_shape():
    d = report_to_dict(AuditReport.clean())
    assert d == {"total_issues": 0, "is_clean": True, "sections": {}}


def test_report_to_dict_flagged_sections_shape():
    text = f"She felt {_BANNED} at once. He ran fast. He jumped high. He sat down. He stood up."
    d = report_to_dict(run_audit(text, _BANK))
    assert d["total_issues"] > 0 and d["is_clean"] is False
    hits = d["sections"]["banned_phrases"]
    assert any(_BANNED in item["phrase"] and item["sentence"] for item in hits)
    openers = d["sections"]["repetitive_openers"]
    assert openers and set(openers[0]) == {"opener", "count", "sentences"}


# ── audit_document ───────────────────────────────────────────────────────────


async def test_audit_clean_draft():
    res = await audit_document("A perfectly ordinary sentence.", "", _BANK, None, assisted=False, truncated=False)
    assert res["skipped"] is None
    assert res["tail_excluded"] is False
    assert res["report"]["is_clean"] is True


async def test_audit_flags_banned_phrase_in_draft():
    res = await audit_document(
        f"She felt {_BANNED} again.", "Earlier document text.", _BANK, None, assisted=False, truncated=False
    )
    assert res["report"]["total_issues"] >= 1
    assert "banned_phrases" in res["report"]["sections"]


async def test_audit_context_findings_are_excluded():
    # The banned phrase lives only in the CONTEXT; the draft is clean, so the
    # draft-narrowed report must be clean (chat-editor filter semantics).
    res = await audit_document(
        "The draft itself is unremarkable.", f"She felt {_BANNED} before.", _BANK, None, assisted=False, truncated=False
    )
    assert res["report"]["is_clean"] is True


async def test_audit_truncated_excludes_tail_fragment():
    # The banned phrase sits in the dangling half-sentence: never flagged.
    draft = f"A clean opening sentence. She felt {_BANNED}"
    res = await audit_document(draft, "", _BANK, None, assisted=False, truncated=True)
    assert res["tail_excluded"] is True
    assert res["report"]["is_clean"] is True


async def test_audit_untruncated_run_is_not_trimmed():
    draft = f"A clean opening sentence. She felt {_BANNED}"
    res = await audit_document(draft, "", _BANK, None, assisted=False, truncated=False)
    assert res["tail_excluded"] is False
    assert res["report"]["total_issues"] >= 1


async def test_audit_single_partial_sentence_skips():
    res = await audit_document("only a fragment with no end", "", _BANK, None, assisted=False, truncated=True)
    assert res["skipped"] == "no_complete_sentence"
    assert res["report"]["is_clean"] is True


async def test_audit_scanner_toggle_off():
    res = await audit_document(
        f"She felt {_BANNED} again.", "", _BANK, {"banned_phrases": False}, assisted=False, truncated=False
    )
    assert res["report"]["is_clean"] is True


# ── patch_document ───────────────────────────────────────────────────────────


class _StubPatchClient:
    """Stub LLMClient: records the forced call, returns canned patches."""

    def __init__(self, patches: list[dict]):
        self.calls: list[dict] = []
        self._patches = patches

    async def complete(self, messages, model, tools=None, tool_choice=None, **params):
        self.calls.append({"messages": messages, "model": model, "tools": tools, "tool_choice": tool_choice, "params": params})
        yield {
            "type": "done",
            "message": {
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {
                            "name": "editor_apply_patch",
                            "arguments": json.dumps({"patches": self._patches}),
                        },
                    }
                ]
            },
        }


async def test_patch_applies_and_reattaches_tail():
    flagged = f"She felt {_BANNED} at once."
    draft = f"{flagged} And then he beg"
    client = _StubPatchClient([{"search": flagged, "replace": "A chill traced her back."}])

    res = await patch_document(client, "m", draft, "", _BANK, None, _SETTINGS, assisted=False, truncated=True)
    assert res["patched_draft"] == "A chill traced her back. And then he beg"
    assert res["patch_count"] == 1
    assert res["errors"] == []
    assert res["report_after"]["is_clean"] is True
    assert res["skipped"] is None

    # One forced editor_apply_patch call on the writer client; the draft core
    # (tail trimmed) rides as the assistant turn the searches must target.
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "function", "function": {"name": "editor_apply_patch"}}
    assert call["tools"][0]["function"]["name"] == "editor_apply_patch"
    assert {"role": "assistant", "content": f"{flagged} "} in call["messages"]
    # Chat-editor parity: reasoning off on the patch call.
    assert call["params"]["reasoning"] == {"effort": "none", "enabled": False}


async def test_patch_errors_surface_and_draft_unchanged():
    draft = f"She felt {_BANNED} at once."
    client = _StubPatchClient([{"search": "text that is not in the draft", "replace": "x"}])

    res = await patch_document(client, "m", draft, "", _BANK, None, _SETTINGS, assisted=False, truncated=False)
    assert res["patched_draft"] == draft
    assert res["patch_count"] == 0
    assert len(res["errors"]) == 1
    assert res["report_after"]["total_issues"] >= 1  # issue still present


async def test_patch_clean_draft_skips_llm():
    client = _StubPatchClient([])
    res = await patch_document(client, "m", "Nothing to fix here.", "", _BANK, None, _SETTINGS, assisted=False, truncated=False)
    assert res["skipped"] == "clean"
    assert res["patched_draft"] == "Nothing to fix here."
    assert client.calls == []  # no LLM call when the audit is already clean


async def test_patch_all_partial_skips_llm():
    client = _StubPatchClient([])
    res = await patch_document(client, "m", "dangling fragm", "", _BANK, None, _SETTINGS, assisted=False, truncated=True)
    assert res["skipped"] == "no_complete_sentence"
    assert res["patched_draft"] == "dangling fragm"
    assert client.calls == []


def test_build_patch_messages_shape():
    msgs = build_patch_messages("The draft core.", "earlier text", "*** REPORT ***")
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]
    assert msgs[2]["content"] == "The draft core."
    assert "earlier text" in msgs[1]["content"]
    assert "*** REPORT ***" in msgs[3]["content"]
