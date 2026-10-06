"""A hostile page cannot reach the server through a visitor's browser; LAN, Tailscale and proxied browsers still can."""

from __future__ import annotations

import pytest

SECRET = "sk-GUARD-SECRET"


@pytest.fixture
async def endpoint_id(client):
    return await client.create(
        "/api/endpoints", json={"name": "guarded", "url": "http://example.invalid/v1", "api_key": SECRET}
    )


async def test_an_open_server_refuses_a_rebound_page(client, endpoint_id):
    response = await client.get(f"/api/endpoints/{endpoint_id}/api-key", headers={"Host": "attacker.example:8899"})

    assert response.status_code == 403
    assert SECRET not in response.text
    assert "ORB_ALLOWED_HOSTS=attacker.example" in response.text


@pytest.mark.parametrize(
    "host",
    ["192.168.1.20:8899", "localhost:8899", "100.101.102.103:8899", "mybox:8899", "mybox.tail1234.ts.net", "mybox.local:8899"],
)
async def test_lan_and_tailscale_browsers_still_reach_an_open_server(client, endpoint_id, host):
    response = await client.get(f"/api/endpoints/{endpoint_id}/api-key", headers={"Host": host})

    assert response.status_code == 200
    assert response.json() == {"api_key": SECRET}


async def test_allowed_hosts_opens_a_custom_name(client, endpoint_id, monkeypatch):
    monkeypatch.setenv("ORB_ALLOWED_HOSTS", "orb.example.com")

    assert (await client.get("/api/settings", headers={"Host": "orb.example.com"})).status_code == 200
    assert (await client.get("/api/settings", headers={"Host": "other.example.com"})).status_code == 403


async def test_a_password_lets_a_signed_in_browser_use_any_name_and_keeps_strangers_anonymous(client):
    await client.put_checked("/api/access/password", json={"password": "hunter2"})
    proxied = {"Host": "orb.example.com"}

    assert (await client.get("/api/settings", headers=proxied)).status_code == 200

    client.cookies.clear()
    stranger = await client.get("/api/settings", headers=proxied)
    assert stranger.status_code == 401  # the gate's anonymous page, not a refusal that names the server


async def test_another_site_cannot_change_anything(client):
    before = await client.get_json("/api/conversations")
    evil = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}

    created = await client.post("/api/conversations", json={}, headers=evil)
    uploaded = await client.post(
        "/api/characters/import", headers=evil, files={"file": ("card.png", b"not-a-png", "image/png")}
    )

    assert created.status_code == uploaded.status_code == 403
    assert "reverse proxy" in created.json()["detail"]
    assert await client.get_json("/api/conversations") == before


async def test_the_apps_own_page_and_its_proxy_can_change_things(client):
    own = await client.put("/api/access/password", json={"password": ""}, headers={"Origin": "http://test"})
    proxied = await client.put(
        "/api/access/password",
        json={"password": ""},
        headers={"Host": "127.0.0.1:8899", "Origin": "https://orb.example.com", "X-Forwarded-Host": "orb.example.com"},
    )

    assert own.status_code == proxied.status_code == 200
