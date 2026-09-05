from __future__ import annotations

import httpx
import pytest

from backend.inference import client as client_module
from backend.inference.client import LLMClient


class _CatalogClient:
    payload: object = {}
    seen: dict = {}

    def __init__(self, **kwargs):
        self.seen["client_kwargs"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers):
        self.seen["url"] = url
        self.seen["headers"] = headers
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=self.payload, request=request)


class _ProbingCatalogClient:
    responses: list[tuple[int, object]] = []
    requests: list[dict] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers):
        self.requests.append({"url": url, "headers": headers})
        status, payload = self.responses.pop(0)
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=payload, request=request)


@pytest.mark.asyncio
async def test_list_models_uses_openai_contract_auth_and_proxy(monkeypatch):
    _CatalogClient.payload = {
        "object": "list",
        "data": [
            {"id": "z-model", "object": "model"},
            {"id": "A-model", "object": "model"},
            {"id": "z-model", "object": "model"},
            {"not-an-id": True},
        ],
    }
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    models = await LLMClient("https://models.test/v1/", "secret-key", proxy="socks5://localhost:1080").list_models()

    assert models == ["A-model", "z-model"]
    assert _CatalogClient.seen["url"] == "https://models.test/v1/models"
    assert _CatalogClient.seen["headers"] == {"Authorization": "Bearer secret-key"}
    assert _CatalogClient.seen["client_kwargs"] == {
        "timeout": 20.0,
        "proxy": "socks5://localhost:1080",
        "follow_redirects": True,
    }


@pytest.mark.asyncio
async def test_list_models_rejects_non_openai_response(monkeypatch):
    _CatalogClient.payload = {"models": ["wrong-shape"]}
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    with pytest.raises(ValueError, match="data list"):
        await LLMClient("https://models.test/v1").list_models()


@pytest.mark.asyncio
async def test_anthropic_models_use_sibling_resource_and_native_auth(monkeypatch):
    _CatalogClient.payload = {"data": [{"id": "claude-opus-5"}]}
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    models = await LLMClient("https://api.anthropic.com/v1/messages", "secret-key").list_models()

    assert models == ["claude-opus-5"]
    assert _CatalogClient.seen["url"] == "https://api.anthropic.com/v1/models"
    assert _CatalogClient.seen["headers"] == {
        "x-api-key": "secret-key",
        "anthropic-version": "2023-06-01",
    }


@pytest.mark.asyncio
async def test_gemini_models_use_normalized_surface_and_strip_models_prefix(monkeypatch):
    _CatalogClient.payload = {"data": [{"id": "models/gemini-3-pro"}, {"id": "gemini-3-flash"}]}
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    models = await LLMClient("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-key").list_models()

    assert models == ["gemini-3-flash", "gemini-3-pro"]
    assert _CatalogClient.seen["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert _CatalogClient.seen["headers"] == {"Authorization": "Bearer gemini-key"}


@pytest.mark.asyncio
async def test_gemini_proxy_catalogue_is_normalized_like_googles_own(monkeypatch):
    # The prefix is a property of the dialect, not of Google's hostname: a proxy
    # mirroring /v1beta/openai relays the same ``models/``-prefixed ids, and
    # leaving them in put an unusable-looking id in the picker.
    _CatalogClient.payload = {"data": [{"id": "models/gemini-3-pro"}]}
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    models = await LLMClient("https://keys.example/v1beta/openai", "k").list_models()

    assert models == ["gemini-3-pro"]
    assert _CatalogClient.seen["url"] == "https://keys.example/v1beta/openai/models"


@pytest.mark.asyncio
async def test_non_gemini_catalogue_keeps_a_models_prefixed_id(monkeypatch):
    _CatalogClient.payload = {"data": [{"id": "models/local-thing"}]}
    _CatalogClient.seen = {}
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _CatalogClient)

    assert await LLMClient("http://localhost:8080/v1", "").list_models() == ["models/local-thing"]


@pytest.mark.asyncio
async def test_ambiguous_catalogue_detection_uses_no_provider_or_model_names(monkeypatch):
    from backend.inference import endpoint_profiles

    endpoint_profiles._RESOLVED_ROUTES.clear()
    _ProbingCatalogClient.requests = []
    _ProbingCatalogClient.responses = [
        (404, {"error": "not found"}),
        (404, {"error": "not found"}),
        (401, {"error": "wrong auth"}),
        (200, {"data": [{"id": "model-7"}]}),
    ]
    monkeypatch.setattr(client_module.httpx, "AsyncClient", _ProbingCatalogClient)

    client = LLMClient("https://opaque.test", "secret-key")
    models = await client.list_models()

    assert models == ["model-7"]
    assert _ProbingCatalogClient.requests == [
        {"url": "https://opaque.test/models", "headers": {"Authorization": "Bearer secret-key"}},
        {
            "url": "https://opaque.test/models",
            "headers": {"x-api-key": "secret-key", "anthropic-version": "2023-06-01"},
        },
        {"url": "https://opaque.test/v1/models", "headers": {"Authorization": "Bearer secret-key"}},
        {
            "url": "https://opaque.test/v1/models",
            "headers": {"x-api-key": "secret-key", "anthropic-version": "2023-06-01"},
        },
    ]
    assert endpoint_profiles.resolve_endpoint(client.base_url, "model-7").protocol == "anthropic"
    assert endpoint_profiles.endpoint_candidates(client.base_url, "model-7") == [
        endpoint_profiles.resolve_endpoint(client.base_url, "model-7")
    ]
