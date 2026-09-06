"""The `offer_tools` blob must be order-stable across sibling forced calls.

image_gen's analyze + compose calls ship one shared array so that a backend
which renders the whole array can serve the second call from the first call's
cached prefix (docs/architecture/kv-cache.md, Invariant 3). That only works if
the array is byte-identical regardless of which member is forced — the sole
difference between the two requests must be `tool_choice`.

Nothing else pins this. `enabled_schemas()` ordering is covered by
test_tool_catalog.py, but the `offer_tools` path bypasses `enabled_schemas`
entirely: it builds the array from the caller's tuple, so a reordered
OFFER_TOOLS or an append-on-miss regression would silently split the two calls
onto different prefixes with no test failing.

Whether the *server* then renders the whole array is a provider property Orb
cannot control or test offline — several backends render only the forced tool.
That is documented, not asserted here. What is asserted is the part Orb owns:
the bytes it sends.
"""

from __future__ import annotations

import json

import pytest

from backend.prompting.tool_catalog import TOOLS
from backend.workflows._forced_call import forced_tool_call
from backend.workflows.image_gen.prompts import OFFER_TOOLS

_SETTINGS = {"model_name": "test-model"}
_PREFIX = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
_TAIL = [{"role": "user", "content": "[OOC] go"}]


class _CapturingClient:
    """Records the wire kwargs of each complete() call; answers the forced tool."""

    base_url = "https://api.example.com/v1"

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, **kw):
        self.calls.append(kw)
        name = kw["tool_choice"]["function"]["name"]

        async def _gen():
            yield {
                "type": "done",
                "message": {"tool_calls": [{"id": "1", "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            }

        return _gen()


async def _run(client, tool_name: str, offer=OFFER_TOOLS) -> None:
    async for _ in forced_tool_call(
        client=client,
        prefix=_PREFIX,
        tail_messages=_TAIL,
        tool_name=tool_name,
        settings=_SETTINGS,
        offer_tools=offer,
    ):
        pass


@pytest.fixture
def client() -> _CapturingClient:
    return _CapturingClient()


async def test_blob_is_byte_identical_across_the_two_forced_calls(client):
    """analyze and compose must differ on tool_choice and nothing else."""
    await _run(client, "analyze_scene")
    await _run(client, "compose_image_prompt")

    analyze, compose = client.calls
    assert json.dumps(analyze["tools"]) == json.dumps(compose["tools"])

    differing = {k for k in analyze.keys() | compose.keys() if analyze.get(k) != compose.get(k)}
    assert differing == {"tool_choice"}


async def test_blob_order_follows_offer_tools_declaration(client):
    """Order is the cache-relevant property; pin it to the declared tuple."""
    await _run(client, "compose_image_prompt")
    names = [t["function"]["name"] for t in client.calls[0]["tools"]]
    assert names == list(OFFER_TOOLS)


async def test_forcing_a_tool_outside_the_offer_appends_it_without_reordering(client):
    """A forced tool absent from offer_tools is appended, never inserted."""
    await _run(client, "direct_scene")
    names = [t["function"]["name"] for t in client.calls[0]["tools"]]
    assert names == [*OFFER_TOOLS, "direct_scene"]


async def test_offer_member_is_not_duplicated_when_forced(client):
    """The forced member already in the array must not be appended twice."""
    await _run(client, "analyze_scene")
    names = [t["function"]["name"] for t in client.calls[0]["tools"]]
    assert names.count("analyze_scene") == 1
    assert len(names) == len(OFFER_TOOLS)


async def test_blob_carries_the_registry_schemas_verbatim(client):
    """The array is the registry's bytes — not a copy that could drift."""
    await _run(client, "analyze_scene")
    sent = client.calls[0]["tools"]
    assert sent == [TOOLS[n]["schema"] for n in OFFER_TOOLS]


async def test_blob_collapses_to_the_forced_tool_when_forcing_is_not_honored(client, monkeypatch):
    """Correctness outranks the cache: an unforced array is a coin flip.

    Guards the branch that trades the shared prefix away — with compose forced
    but coerced, deepseek-v4-pro answered analyze_scene 8/8.
    """
    monkeypatch.setattr(
        "backend.workflows._forced_call.honors_forced_tool_choice",
        lambda *a, **k: False,
    )
    await _run(client, "compose_image_prompt")
    names = [t["function"]["name"] for t in client.calls[0]["tools"]]
    assert names == ["compose_image_prompt"]
