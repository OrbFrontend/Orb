"""Card sources answer what the remote site sent: a page that is not a JSON object is the site's failure (502), never Orb's
(a bare 500), and one malformed field does not sink a whole search or import."""

from collections import OrderedDict

import httpx
import pytest
from fastapi import HTTPException

from backend.features.cards import downloader


@pytest.fixture(autouse=True)
def _isolated_page_counts(monkeypatch):
    monkeypatch.setattr(downloader, "_page_counts", OrderedDict())


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


async def test_a_chub_result_carries_the_sites_tallies_but_not_an_unrated_score(monkeypatch):
    rated = {"name": "Amy", "fullPath": "maker/amy", "rating": 4, "ratingCount": 65, "starCount": 50972, "n_favorites": 5956}
    unrated = {"name": "Bea", "fullPath": "maker/bea", "rating": 5, "ratingCount": 0, "starCount": "lots"}
    _serve(monkeypatch, lambda request: httpx.Response(200, json={"data": {"nodes": [rated, unrated], "count": 2}}))
    amy, bea = (await downloader.browse("characterhub", "", 1))["results"]
    assert (amy["creator"], amy["rating"], amy["downloads"], amy["favorites"]) == ("maker", 4.0, 50972, 5956)
    assert (bea["rating"], bea["rating_count"], bea["downloads"]) == (None, None, None)


def _catalog(source, pages, name="Fresh"):
    if source == "characterhub":
        return {"data": {"nodes": [{"name": name, "fullPath": "maker/amy"}], "count": pages * 24}}
    if source == "chararc":
        return {"result": [{"name": name, "source": "chub", "chub": {"fullPath": "maker/amy"}}], "totalPages": pages}
    return {"results": [{"name": name, "id": "amy"}], "totalPages": pages}


@pytest.mark.parametrize("source", ["characterhub", "chararc", "wyvern"])
async def test_randomize_reuses_browse_bounds_but_fetches_fresh_cards(monkeypatch, source):
    pages = []

    def site(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=_catalog(source, 5, name=f"Fresh {len(pages)}"))

    _serve(monkeypatch, site)
    monkeypatch.setattr(downloader.random, "randint", lambda low, high: high)
    await downloader.browse(source)
    first = await downloader.randomize(source)
    second = await downloader.randomize(source)
    assert pages == [1, 5, 5]
    assert first["results"][0]["name"] == "Fresh 2"
    assert second["results"][0]["name"] == "Fresh 3"
    assert first["has_more"] is second["has_more"] is False


async def test_wyvern_bounds_expire_and_do_not_cross_queries_or_accounts(monkeypatch):
    calls = []

    async def bearer(token, **kwargs):
        return token

    def site(request):
        query = request.url.params.get("q", "")
        auth = request.headers.get("Authorization")
        page = int(request.url.params["page"])
        calls.append((query, auth, page))
        count = 2 if auth is None and not query else 4
        return httpx.Response(200, json=_catalog("wyvern", count))

    _serve(monkeypatch, site)
    monkeypatch.setattr(downloader, "_wyvern_id_token", bearer)
    monkeypatch.setattr(downloader.random, "randint", lambda low, high: high)
    await downloader.randomize("wyvern")
    await downloader.randomize("wyvern")
    await downloader.randomize("wyvern", "elf")
    await downloader.randomize("wyvern", token="member")
    assert calls == [
        ("", None, 1),
        ("", None, 2),
        ("", None, 2),
        ("elf", None, 1),
        ("elf", None, 4),
        ("", "Bearer member", 1),
        ("", "Bearer member", 4),
    ]
    monkeypatch.setattr(downloader, "_PAGE_COUNT_TTL", 0)
    await downloader.browse("wyvern")
    calls.clear()
    await downloader.randomize("wyvern")
    assert calls == [("", None, 1), ("", None, 2)]


@pytest.mark.parametrize("source", ["characterhub", "chararc", "wyvern"])
async def test_a_shrinking_catalog_recovers_from_stale_random_bounds(monkeypatch, source):
    pages = []

    def site(request):
        page = int(request.url.params["page"])
        pages.append(page)
        data = _catalog(source, 5 if len(pages) == 1 else 1)
        if page > 1:
            if source == "characterhub":
                data["data"]["nodes"] = []
            else:
                data["result" if source == "chararc" else "results"] = []
        return httpx.Response(200, json=data)

    _serve(monkeypatch, site)
    monkeypatch.setattr(downloader.random, "randint", lambda low, high: high)
    await downloader.browse(source)
    assert (await downloader.randomize(source))["results"]
    assert (await downloader.randomize(source))["results"]
    assert pages == [1, 5, 1, 1]


async def test_shared_connections_keep_auth_per_request_discard_cookies_and_close(monkeypatch):
    requests = []
    clients = []
    real = httpx.AsyncClient

    def site(request):
        requests.append(request)
        return httpx.Response(200, headers={"set-cookie": "session=member; Path=/"}, json={"posts": [], "total": 0})

    def create(**kwargs):
        client = real(transport=httpx.MockTransport(site), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create)
    async with downloader.http_session():
        await downloader.randomize("botbooru", token="member")
        await downloader.randomize("botbooru")
        assert len(clients) == 1
        assert not clients[0].is_closed
    assert clients[0].is_closed
    assert requests[0].headers["Authorization"] == "Bearer member"
    assert "Authorization" not in requests[1].headers
    assert "Cookie" not in requests[1].headers
    assert downloader._http_client is None
