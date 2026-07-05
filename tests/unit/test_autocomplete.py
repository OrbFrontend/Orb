"""Autocomplete: Tier-1 pure trimmer (always runs) + Tier-2 real weights (opt-in).

Tier-2 loads the actual GGUF and needs `pip install -r requirements-ml.txt`; it
skips cleanly when the extra or the model file is absent, so the default suite
stays green without ML deps.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from backend.inference import local_ml as lc

# ── Tier 1: pure prompt trimmer, no model, no DB ────────────────────────────


def test_build_prompt_ends_at_draft_and_excludes_injection():
    p = lc.build_prompt(
        "Aria",
        "Sam",
        "Aria is a wry tavern keeper.",
        [
            {"role": "assistant", "content": "You look lost."},
            {"role": "user", "content": "Maybe I am."},
        ],
        "I walk into the",
    )
    assert p.endswith("Sam: I walk into the")  # model continues this exact line
    assert "Aria: You look lost." in p
    assert "Sam: Maybe I am." in p
    assert "Aria is a wry tavern keeper." in p
    # Lightweight typeahead — the Director/pipeline injection block must not leak in.
    assert "Director" not in p and "Scene Direction" not in p


def test_build_prompt_truncates_long_message():
    p = lc.build_prompt("A", "U", "", [{"role": "user", "content": "x" * 2000}], "hi")
    assert "x" * 501 not in p  # capped at max_msg_chars=500


def test_build_prompt_skips_empty_summary_and_messages():
    p = lc.build_prompt("A", "U", "  ", [{"role": "user", "content": "  "}], "go")
    assert p == "U: go"


# ── Tier 2: real weights (opt-in) ───────────────────────────────────────────


def test_real_model_completes():
    pytest.importorskip("llama_cpp", reason="opt-in: needs requirements-ml.txt")
    if not os.path.exists(lc.resolve_path("autocomplete")):
        pytest.skip(f"GGUF not on disk: {lc.resolve_path('autocomplete')}")

    prompt = lc.build_prompt(
        "Aria",
        "Sam",
        "Aria is a wry tavern keeper.",
        [{"role": "assistant", "content": "You arrive at the gate."}],
        "I walk into the",
    )
    t0 = time.perf_counter()
    out = asyncio.run(lc.complete(prompt, n_predict=12))
    dt_ms = (time.perf_counter() - t0) * 1000
    # Informational only — CPU timing is machine-dependent, so not asserted.
    print(f"\n[autocomplete] {dt_ms:.0f}ms for ~12 tokens -> {out!r}")
    assert isinstance(out, str)
    assert out.strip() != ""
    assert len(out) < 400
