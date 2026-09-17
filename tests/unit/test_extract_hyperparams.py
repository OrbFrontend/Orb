"""The lane cascade `extract_hyperparams` applies to a settings row."""

from __future__ import annotations

import pytest

from backend.core import agent_lane_cut_off, agent_lane_max_tokens, extract_hyperparams

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


def test_the_budget_goes_out_as_configured():
    # No call raises it: the setting a user can see is the budget every call sends.
    assert extract_hyperparams({"max_tokens": 600}) == {"max_tokens": 600}
    assert extract_hyperparams({"max_tokens": 600, "agent_max_tokens": 32768}, lane="agent")["max_tokens"] == 32768


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"agent_max_tokens": 32768, "max_tokens": 600}, 32768),
        ({"max_tokens": 600}, 600),
        ({}, 4096),
    ],
)
def test_agent_lane_max_tokens_is_the_configured_budget(settings, expected):
    assert agent_lane_max_tokens(settings) == expected


@pytest.mark.parametrize(
    ("settings", "sentence"),
    [
        ({"agent_max_tokens": 2048, "max_tokens": 600}, "The model's reply was cut off at the Agent Max Tokens limit of 2048."),
        ({"max_tokens": 600}, "The model's reply was cut off at the Max Tokens limit of 600."),
    ],
)
def test_a_cut_off_names_the_setting_the_budget_came_from(settings, sentence):
    assert agent_lane_cut_off(settings) == sentence
