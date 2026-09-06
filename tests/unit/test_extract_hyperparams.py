"""The lane cascade and token floor `extract_hyperparams` applies to a settings row."""

from __future__ import annotations

import pytest

from backend.core import agent_lane_max_tokens, extract_hyperparams

_WRITER = {"temperature": 0.8, "max_tokens": 4096, "top_p": 0.95, "min_p": 0.0, "top_k": 40, "repetition_penalty": 1.0}


def test_the_writer_lane_never_reads_the_agent_overlay():
    params = extract_hyperparams({**_WRITER, "agent_temperature": 0.2, "agent_max_tokens": 512})
    assert params == _WRITER


def test_the_agent_lane_prefers_its_own_values():
    settings = {**_WRITER, "agent_temperature": 0.2, "agent_max_tokens": 512}
    params = extract_hyperparams(settings, lane="agent")
    assert params["temperature"] == 0.2
    assert params["max_tokens"] == 512


def test_a_partial_mapping_falls_back_per_key():
    # A real settings row is all-or-nothing -- every `agent_` twin comes from the
    # same overlay of six NOT NULL columns -- so this guards callers that hand in a
    # hand-built mapping rather than a state the database can reach.
    params = extract_hyperparams({**_WRITER, "agent_temperature": 0.2}, lane="agent")
    assert params == {**_WRITER, "temperature": 0.2}


def test_defaults_only_fill_keys_no_lane_supplied():
    params = extract_hyperparams({"temperature": 0.8}, lane="agent", defaults={"temperature": 0.25, "max_tokens": 2048})
    assert params == {"temperature": 0.8, "max_tokens": 2048}


class TestTokenFloor:
    """The configured budget may raise the floor; it may never lower it."""

    def test_a_roomier_budget_is_kept(self):
        assert extract_hyperparams({"max_tokens": 16384}, token_floor=8192)["max_tokens"] == 16384

    def test_a_short_reply_budget_is_raised_to_the_floor(self):
        # 600 tokens is a normal setting for brief prose. Sending it to a call whose
        # whole answer must fit truncates the answer, which reaches the user as the
        # pass doing nothing rather than as the shorter reply they asked for.
        assert extract_hyperparams({"max_tokens": 600}, token_floor=8192)["max_tokens"] == 8192

    def test_a_missing_budget_becomes_the_floor(self):
        assert extract_hyperparams({}, token_floor=2048)["max_tokens"] == 2048

    def test_the_floor_outranks_a_default(self):
        params = extract_hyperparams({}, token_floor=8192, defaults={"max_tokens": 512})
        assert params["max_tokens"] == 8192

    def test_no_floor_leaves_the_budget_alone(self):
        # Prose passes stream to a stop token, so a short preset is honored there.
        assert extract_hyperparams({"max_tokens": 600}) == {"max_tokens": 600}

    def test_the_floor_reads_the_agent_lane(self):
        settings = {"max_tokens": 600, "agent_max_tokens": 32768}
        assert extract_hyperparams(settings, lane="agent", token_floor=8192)["max_tokens"] == 32768


@pytest.mark.parametrize(
    ("settings", "floor", "expected"),
    [
        ({"agent_max_tokens": 32768, "max_tokens": 600}, 8192, 32768),
        ({"max_tokens": 600}, 8192, 8192),
        ({}, 4096, 4096),
    ],
)
def test_agent_lane_max_tokens_is_the_budget_alone(settings, floor, expected):
    assert agent_lane_max_tokens(settings, floor=floor) == expected
