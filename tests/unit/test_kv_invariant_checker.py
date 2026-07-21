"""Self-check for ``verify_kv_prefix_invariants`` — one case per failure class.

Synthetic ``captured`` entries in the FakeLLMClient shape; no app stack. If the
checker logic breaks, these fail before the integration suite quietly loses its
default-on KV-cache guarantee.
"""

from __future__ import annotations

from tests.integration._llm_mock import verify_kv_prefix_invariants

_SYS = {"role": "system", "content": "You are Iris."}
_SYS_DRIFTED = {"role": "system", "content": "You are Iris.\n\n## Lorebook\nThe moon is shattered."}
_GREET = {"role": "assistant", "content": "The archive is quiet tonight."}
_TOOLS = [{"type": "function", "function": {"name": "direct_scene"}}]
_TOOLS_SINGLE = [{"type": "function", "function": {"name": "analyze_scene"}}]


def _call(system=_SYS, tools=_TOOLS, model="m", endpoint="http://one", params=None, tail=()):
    return {
        "pass": "writer",
        "model": model,
        "endpoint": endpoint,
        "messages": [system, _GREET, *tail],
        "tools": tools,
        "params": params or {},
    }


def test_consistent_calls_pass():
    assert verify_kv_prefix_invariants([_call(), _call(tail=({"role": "user", "content": "hi"},))]) == []


def test_system_drift_is_flagged():
    # The off-turn-prefix leak class: a second builder omits a system section.
    violations = verify_kv_prefix_invariants([_call(system=_SYS_DRIFTED), _call()])
    assert len(violations) == 1 and "system" in violations[0]


def test_tools_blob_drift_is_flagged():
    # The forced-call leak class: a per-call single-tool array in the prompt.
    violations = verify_kv_prefix_invariants([_call(), _call(tools=_TOOLS_SINGLE)])
    assert len(violations) == 1 and "tools" in violations[0]


def test_tools_out_of_prompt_are_exempt():
    # The consolidation fix's contract: tools_in_prompt=False never renders.
    assert verify_kv_prefix_invariants([_call(), _call(tools=_TOOLS_SINGLE, params={"tools_in_prompt": False})]) == []


def test_empty_vs_nonempty_blob_is_flagged():
    # The original magic_rewrite tools=None bust.
    assert verify_kv_prefix_invariants([_call(), _call(tools=None)])


def test_models_are_separate_cache_lanes():
    assert verify_kv_prefix_invariants([_call(), _call(model="agent", tools=None)]) == []


def test_servers_are_separate_cache_lanes():
    # Dual-model: writer and agent servers hold independent KV caches, and both
    # auto-provisioned model configs may share a name — the endpoint splits them.
    assert verify_kv_prefix_invariants([_call(), _call(endpoint="http://agent.local", tools=None, system=_SYS_DRIFTED)]) == []


def test_different_conversations_and_singletons_are_skipped():
    other_greet = {"role": "system", "content": "You are Ashley."}, {"role": "assistant", "content": "Hey."}
    a = _call()
    b = {"pass": "writer", "model": "m", "messages": list(other_greet), "tools": _TOOLS, "params": {}}
    assert verify_kv_prefix_invariants([a, b]) == []
    assert verify_kv_prefix_invariants([a]) == []
    assert (
        verify_kv_prefix_invariants([{"pass": "writer", "model": "m", "messages": [_SYS], "tools": _TOOLS, "params": {}}]) == []
    )
