"""A card-site sign-in widens what the site lists: the saved session rides every browse until the site rejects it, and the
token itself never leaves the backend through the settings payload."""

from __future__ import annotations

import base64
import json
import time

import httpx

_TOKEN = (
    "header." + base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=") + ".sig"
)


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
