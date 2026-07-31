"""
Per-endpoint proxy contracts.

Proxy lives on the ``endpoints`` row (next to ``url`` / ``api_key``). Three seams:
  * ``LLMClient`` normalizes the stored value ("" = no proxy) to what httpx wants
    (None for a direct connection; httpx rejects "" as a URL).
  * ``EndpointUpdate`` gates the scheme at save time so a typo fails on save, not
    on every LLM turn. httpx 0.27 accepts http/https/socks5 (socks5 via the
    httpx[socks] extra) and nothing else.
  * ``client_from_settings`` / ``agent_client_from_settings`` thread the
    get_settings() overlay keys (``proxy`` / ``agent_proxy``) into the client.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.api.schemas import EndpointUpdate
from backend.inference import (
    LLMClient,
    agent_client_from_settings,
    client_from_settings,
)


def test_llmclient_proxy_default_is_none():
    assert LLMClient("http://localhost:9999").proxy is None


def test_llmclient_empty_proxy_normalizes_to_none():
    # The stored default is "" (no proxy); httpx rejects "" as a URL, so it must
    # reach httpx as None (direct connection).
    assert LLMClient("http://localhost:9999", proxy="").proxy is None


def test_llmclient_preserves_real_proxy():
    c = LLMClient("http://localhost:9999", proxy="socks5://127.0.0.1:1080")
    assert c.proxy == "socks5://127.0.0.1:1080"


@pytest.mark.parametrize(
    "url",
    ["http://proxy:8080", "https://proxy:8443", "socks5://127.0.0.1:1080"],
)
def test_endpoint_update_accepts_supported_schemes(url):
    assert EndpointUpdate(proxy=url).proxy == url


def test_endpoint_update_blank_proxy_becomes_empty():
    assert EndpointUpdate(proxy="   ").proxy == ""


def test_endpoint_update_trims_proxy():
    assert EndpointUpdate(proxy="  socks5://h:1  ").proxy == "socks5://h:1"


def test_endpoint_update_proxy_unset_is_none():
    # Omitted != blank: model_dump(exclude_unset=True) drops it, so the PUT leaves
    # the endpoint's proxy column untouched.
    assert EndpointUpdate().proxy is None


@pytest.mark.parametrize("url", ["socks5h://h:1", "socks4://h:1", "ftp://h:1", "not-a-url"])
def test_endpoint_update_rejects_unsupported_schemes(url):
    # socks5h/socks4 are rejected too -- httpx 0.27 does not accept them, so the
    # allowlist must match what httpx can actually build.
    with pytest.raises(ValidationError):
        EndpointUpdate(proxy=url)


def test_client_from_settings_threads_proxy():
    s = {"endpoint_url": "http://w:1", "proxy": "socks5://127.0.0.1:1080"}
    assert client_from_settings(s).proxy == "socks5://127.0.0.1:1080"


def test_client_from_settings_empty_proxy_is_none():
    s = {"endpoint_url": "http://w:1", "proxy": ""}
    assert client_from_settings(s).proxy is None


def test_agent_client_uses_agent_proxy():
    s = {
        "endpoint_url": "http://w:1",
        "agent_endpoint_url": "http://a:1",
        "proxy": "socks5://writer:1",
        "agent_proxy": "socks5://agent:2",
    }
    assert agent_client_from_settings(s).proxy == "socks5://agent:2"


def test_agent_client_falls_back_to_writer_proxy():
    # No agent_proxy key: the agent client inherits the writer's proxy, mirroring
    # how agent_endpoint_url falls back to endpoint_url in the same factory.
    s = {"endpoint_url": "http://w:1", "proxy": "socks5://writer:1"}
    assert agent_client_from_settings(s).proxy == "socks5://writer:1"
