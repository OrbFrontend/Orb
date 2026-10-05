"""A card-site sign-in widens what the site lists: the saved session rides every browse until the site rejects it, and the
token itself never leaves the backend through the settings payload."""

from __future__ import annotations

import json

import httpx

_TOKEN = "botbooru-session"


class _FakeBotbooru:
    def __init__(self) -> None:
        self.accept_token = True
        self.post_auth: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization")
        if request.url.path == "/auth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("password") != "right":
                return httpx.Response(401, json={"detail": "Incorrect username or password"})
            return httpx.Response(200, json={"access_token": _TOKEN, "token_type": "bearer"})
        if request.url.path == "/auth/me":
            if self.accept_token and auth == f"Bearer {_TOKEN}":
                return httpx.Response(200, json={"username": "orbfrontend"})
            return httpx.Response(401, json={"detail": "Could not validate credentials"})
        if request.url.path == "/posts/":
            self.post_auth.append(auth)
            return httpx.Response(200, json={"posts": [], "total": 0})
        return httpx.Response(404)


async def test_botbooru_sign_in_rides_browses_until_the_site_rejects_it(client, monkeypatch):
    site = _FakeBotbooru()
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(site), **kwargs))
    account_url = "/api/characters/sources/botbooru/account"
    login_url = "/api/characters/sources/botbooru/login"

    assert (await client.get_json(account_url))["username"] is None
    refused = await client.post_checked(login_url, json={"username": "orbfrontend", "password": "wrong"}, expected_status=400)
    assert refused.json()["detail"] == "Incorrect username or password"

    signed_in = await client.post_json(login_url, json={"username": "orbfrontend", "password": "right"})
    assert signed_in["username"] == "orbfrontend"
    assert _TOKEN not in (await client.get_checked("/api/settings")).text

    await client.get_checked("/api/characters/browse", params={"source": "botbooru"})
    await client.get_checked("/api/characters/randomize", params={"source": "botbooru"})
    assert site.post_auth == [f"Bearer {_TOKEN}"] * 2

    # Revoked on the site: the status check drops the login, and browsing falls back to a guest's view.
    site.accept_token = False
    assert await client.get_json(account_url) == {"supported": True, "username": None, "expired": True}
    await client.get_checked("/api/characters/browse", params={"source": "botbooru"})
    assert site.post_auth[-1] is None

    assert (await client.get_json("/api/characters/sources/chararc/account"))["supported"] is False


class _FakeWyvern:
    """Firebase Auth hands out a refresh token and hour-long ID tokens; the Wyvern API reads the ID token as its bearer."""

    def __init__(self) -> None:
        self.accept_refresh = True
        self.refreshes = 0
        self.search_auth: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/accounts:signInWithPassword":
            if json.loads(request.content).get("password") != "right":
                return httpx.Response(400, json={"error": {"code": 400, "message": "INVALID_LOGIN_CREDENTIALS"}})
            return httpx.Response(200, json={"displayName": "Jackel", "idToken": "id-1", "refreshToken": "refresh-1"})
        if request.url.path == "/v1/token":
            self.refreshes += 1
            if not self.accept_refresh or "refresh_token=refresh-1" not in request.content.decode():
                return httpx.Response(400, json={"error": {"code": 400, "message": "INVALID_REFRESH_TOKEN"}})
            return httpx.Response(200, json={"id_token": f"id-{self.refreshes + 1}"})
        if request.url.path == "/exploreSearch/characters":
            self.search_auth.append(request.headers.get("Authorization"))
            return httpx.Response(200, json={"results": [], "hasMore": False, "totalPages": 1})
        return httpx.Response(404)


async def test_wyvern_sign_in_keeps_the_refresh_token_and_browses_with_id_tokens(client, monkeypatch):
    from backend.features.cards import downloader

    site = _FakeWyvern()
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(site), **kwargs))
    monkeypatch.setattr(downloader, "_wyvern_id_tokens", {})
    account_url = "/api/characters/sources/wyvern/account"
    login_url = "/api/characters/sources/wyvern/login"

    refused = await client.post_checked(login_url, json={"username": "a@b.c", "password": "wrong"}, expected_status=400)
    assert refused.json()["detail"] == "Invalid login credentials"

    assert (await client.post_json(login_url, json={"username": "a@b.c", "password": "right"}))["username"] == "Jackel"
    assert "refresh-1" not in (await client.get_checked("/api/settings")).text

    # The sign-in's own ID token serves both calls; nothing is renewed while it is fresh.
    await client.get_checked("/api/characters/browse", params={"source": "wyvern"})
    await client.get_checked("/api/characters/randomize", params={"source": "wyvern"})
    assert site.search_auth == ["Bearer id-1"] * 2
    assert site.refreshes == 0

    # The status check renews the session, and browsing picks up the new ID token.
    assert (await client.get_json(account_url))["username"] == "Jackel"
    await client.get_checked("/api/characters/browse", params={"source": "wyvern"})
    assert site.search_auth[-1] == "Bearer id-2"

    # Password changed on the site: the refresh token dies, the login is dropped, and browsing is a guest's.
    site.accept_refresh = False
    assert await client.get_json(account_url) == {"supported": True, "username": None, "expired": True}
    await client.get_checked("/api/characters/browse", params={"source": "wyvern"})
    assert site.search_auth[-1] is None
