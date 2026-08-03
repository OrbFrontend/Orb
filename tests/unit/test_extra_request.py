"""Per-model extra headers/body: parsers, save-time validation, merge, factory threading."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

import backend.inference.client as llm_mod
from backend.api.schemas import ModelConfigCreate, ModelConfigUpdate
from backend.inference.client import (
    LLMClient,
    agent_client_from_settings,
    client_from_settings,
    parse_extra_body,
    parse_extra_headers,
)

# Built by codepoint so the file stays byte-ASCII and the invisible characters
# are visible in source.
NBSP = chr(0xA0)
ACCENT = chr(0xE9)


# --- parsers ---------------------------------------------------------------


def test_headers_parse_lines_and_skip_noise():
    text = "X-Provider: deepinfra\n\n# a comment\nX-Billing-Mode: paygo"
    assert parse_extra_headers(text) == {"X-Provider": "deepinfra", "X-Billing-Mode": "paygo"}


def test_headers_keep_colons_in_the_value():
    # Only the first colon separates; a URL value must survive intact.
    assert parse_extra_headers("X-Ref: https://example.com:8443/x") == {"X-Ref": "https://example.com:8443/x"}


def test_headers_drop_malformed_lines_without_raising():
    # A gameplay path never dies over a parser-level issue: keep the good line.
    assert parse_extra_headers("X-Ok: 1\nno colon here\nBad Name: v") == {"X-Ok": "1"}


def test_headers_empty_input():
    assert parse_extra_headers("") == {}
    assert parse_extra_headers("   \n\n") == {}


def test_headers_strip_the_name_before_checking_it():
    # A space before the colon is a typo, not a bad header name.
    assert parse_extra_headers("X-Provider : deepinfra") == {"X-Provider": "deepinfra"}


def test_headers_drop_non_ascii_value():
    # httpx raises UnicodeEncodeError when it builds the request, and that is
    # not an httpx.HTTPError, so the retry loop never sees it.
    assert parse_extra_headers(f"X-Ok: 1\nX-Bad: caf{ACCENT}") == {"X-Ok": "1"}


def test_headers_drop_non_ascii_name():
    assert parse_extra_headers(f"X-Ok: 1\nX-Caf{ACCENT}: v") == {"X-Ok": "1"}


def test_headers_drop_ascii_name_outside_the_token_set():
    # The reachable case: a header pasted the way provider docs render it in
    # JSON. h11 rejects the quoted name on send with a non-retryable error.
    assert parse_extra_headers('"X-Provider": "deepinfra"') == {}


def test_headers_drop_control_character_in_value():
    assert parse_extra_headers("X-Bad: a\x00b") == {}


def test_headers_accept_a_non_breaking_space_separator():
    # Copying from an HTML docs page yields U+00A0 in place of the separator
    # space. It is discarded with the rest of the separator whitespace, so the
    # pair that reaches httpx is pure ASCII and must not be rejected.
    assert parse_extra_headers(f"X-Provider:{NBSP}deepinfra") == {"X-Provider": "deepinfra"}


def test_body_parses_object_only():
    assert parse_extra_body('{"provider": {"only": ["x"]}, "seed": 7}') == {"provider": {"only": ["x"]}, "seed": 7}
    assert parse_extra_body("[1, 2]") == {}  # not an object: nothing to merge
    assert parse_extra_body("{oops") == {}
    assert parse_extra_body("") == {}


# --- save-time validation --------------------------------------------------


def test_update_accepts_well_formed():
    assert ModelConfigUpdate(extra_headers="X-Provider: deepinfra").extra_headers == "X-Provider: deepinfra"
    assert ModelConfigUpdate(extra_body='{"seed": 1}').extra_body == '{"seed": 1}'
    assert ModelConfigUpdate(extra_headers="  ").extra_headers == ""


def test_update_accepts_stripped_name_and_separator_whitespace():
    assert ModelConfigUpdate(extra_headers="X-Provider : deepinfra").extra_headers == "X-Provider : deepinfra"
    nbsp_line = f"X-Provider:{NBSP}deepinfra"
    assert ModelConfigUpdate(extra_headers=nbsp_line).extra_headers == nbsp_line


MALFORMED = [
    {"extra_headers": "X-Provider deepinfra"},  # no colon
    {"extra_headers": "Bad Name: v"},  # whitespace in the name
    {"extra_headers": '"X-Provider": "deepinfra"'},  # ASCII, but not an HTTP token
    {"extra_headers": f"X-Bad: caf{ACCENT}"},  # non-ASCII value
    {"extra_body": "[1,2]"},  # not an object
    {"extra_body": "{nope"},  # not JSON
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_update_rejects_malformed(payload):
    with pytest.raises(ValidationError):
        ModelConfigUpdate(**payload)


@pytest.mark.parametrize("payload", MALFORMED)
def test_create_rejects_malformed(payload):
    # model_name is required with no default, so a partial payload would raise
    # for the missing field whether or not the new validators exist.
    with pytest.raises(ValidationError):
        ModelConfigCreate(model_name="m", **payload)


def test_create_accepts_well_formed():
    mc = ModelConfigCreate(model_name="m", extra_headers="X-Provider : deepinfra", extra_body='{"seed": 1}')
    assert mc.extra_headers == "X-Provider : deepinfra"
    assert mc.extra_body == '{"seed": 1}'


# --- client merge ----------------------------------------------------------


def test_client_defaults_are_empty():
    c = LLMClient("http://localhost:9999")
    assert c.extra_headers == {}
    assert c.extra_body == {}
    assert c._headers() == {}


def test_headers_merge_over_authorization():
    c = LLMClient("http://x", api_key="sk-1", extra_headers="X-Provider: deepinfra")
    assert c._headers() == {"Authorization": "Bearer sk-1", "X-Provider": "deepinfra"}


def test_headers_may_replace_authorization():
    # Deliberate: a gateway wanting a different auth scheme is exactly the kind
    # of thing an escape hatch exists for.
    c = LLMClient("http://x", api_key="sk-1", extra_headers="Authorization: Custom xyz")
    assert c._headers() == {"Authorization": "Custom xyz"}


def test_headers_replace_authorization_case_insensitively():
    # Lowercase is the form most provider docs use. Sending both would leak the
    # Bearer key to the gateway the override exists to hide it from.
    c = LLMClient("http://x", api_key="sk-1", extra_headers="authorization: Custom xyz")
    assert c._headers() == {"authorization": "Custom xyz"}


# --- wire-level body merge -------------------------------------------------


class _FakeStream:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'
        yield "data: [DONE]"


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        self.bodies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.bodies.append(dict(json or {}))
        return _FakeStream()


async def _wire_body(client: LLMClient, **params) -> dict:
    fake = _FakeAsyncClient()
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake):
        async for _ in client.complete([], "m", **params):
            pass
    assert len(fake.bodies) == 1
    return fake.bodies[0]


async def test_wire_extra_body_reaches_the_request():
    client = LLMClient("http://localhost:5000/v1", extra_body='{"provider": {"only": ["deepinfra"]}}')
    body = await _wire_body(client)
    assert body["provider"] == {"only": ["deepinfra"]}


async def test_wire_extra_body_overrides_a_computed_key():
    client = LLMClient("http://localhost:5000/v1", extra_body='{"temperature": 0.1}')
    body = await _wire_body(client, temperature=0.9)
    assert body["temperature"] == 0.1


# --- settings threading ----------------------------------------------------


def test_agent_falls_back_to_writer_values():
    settings = {
        "endpoint_url": "http://x",
        "model_name": "m",
        "extra_headers": "X-Provider: deepinfra",
        "extra_body": '{"seed": 7}',
    }
    for build in (client_from_settings, agent_client_from_settings):
        c = build(settings)
        assert c.extra_headers == {"X-Provider": "deepinfra"}
        assert c.extra_body == {"seed": 7}


def test_agent_overrides_writer_values():
    settings = {
        "endpoint_url": "http://x",
        "model_name": "m",
        "extra_headers": "X-Provider: writerprov",
        "agent_extra_headers": "X-Provider: agentprov",
        "extra_body": '{"seed": 1}',
        "agent_extra_body": '{"seed": 2}',
    }
    assert client_from_settings(settings).extra_headers == {"X-Provider": "writerprov"}
    assert agent_client_from_settings(settings).extra_headers == {"X-Provider": "agentprov"}
    assert client_from_settings(settings).extra_body == {"seed": 1}
    assert agent_client_from_settings(settings).extra_body == {"seed": 2}
