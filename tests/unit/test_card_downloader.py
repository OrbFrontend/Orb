"""Card sources answer what the remote site sent: a page that is not a JSON object is the site's failure (502), never Orb's
(a bare 500), and one malformed field does not sink a whole search or import."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from backend.features.cards import downloader


def _serve(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=transport, **kwargs))


@pytest.mark.parametrize("source", sorted(downloader.SOURCES))
@pytest.mark.parametrize("body", ["<html>Just a moment...</html>", "[]"])
async def test_a_search_answered_without_a_json_object_is_a_bad_gateway(monkeypatch, source, body):
    _serve(monkeypatch, lambda request: httpx.Response(200, text=body))
    with pytest.raises(HTTPException) as caught:
        await downloader.browse(source, "elf", 1)
    assert caught.value.status_code == 502


async def test_a_chub_result_with_a_null_description_still_lists(monkeypatch):
    node = {"name": "Amy", "fullPath": "someone/amy", "tagline": None, "description": None}
    _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": {"nodes": [node], "count": 1}}))
    page = await downloader.browse("characterhub", "", 1)
    assert [(r["name"], r["tagline"]) for r in page["results"]] == [("Amy", "")]


async def test_a_malformed_chub_expression_pack_is_skipped_not_raised(monkeypatch):
    _serve(monkeypatch, lambda request: httpx.Response(200, json={"node": {"definition": "not an object"}}))
    assert await downloader._chub_expression_pack("someone/amy") is None
