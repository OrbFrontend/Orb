"""The access password gate."""

import itertools
import sqlite3
from contextlib import closing

import httpx
import pytest
from httpx import ASGITransport

_hosts = itertools.count(1)


@pytest.fixture
async def browser(client):
    from backend.main import app

    opened: list[httpx.AsyncClient] = []

    def open_browser() -> httpx.AsyncClient:
        transport = ASGITransport(app=app, client=(f"10.0.0.{next(_hosts)}", 50000))
        opened.append(httpx.AsyncClient(transport=transport, base_url="http://test"))
        return opened[-1]

    yield open_browser
    for b in opened:
        await b.aclose()


async def _sign_in(browser: httpx.AsyncClient, password: str, path: str = "/") -> httpx.Response:
    return await browser.post(path, data={"password": password})


async def test_locked_app_answers_every_request_with_one_anonymous_page(client, browser):
    await client.put_checked("/api/access/password", json={"password": "hunter2"})
    stranger = browser()

    responses = [
        await stranger.get("/"),
        await stranger.get("/api/settings"),
        await stranger.get("/api/endpoints"),
        await stranger.get("/static/app.js"),
        await stranger.get("/static/favicon.svg"),
        await stranger.get("/docs"),
        await stranger.get("/openapi.json"),
        await stranger.get("/no-such-page"),
        await stranger.post("/api/conversations", json={}),
        await stranger.put("/api/access/password", json={"password": ""}),
        await stranger.get("/", headers={"Cookie": "session=forged"}),
        await stranger.get("/", headers={"Cookie": "session=\u00e9".encode()}),
    ]

    assert {r.status_code for r in responses} == {401}
    assert len({r.content for r in responses}) == 1
    assert len({tuple(sorted(r.headers.items())) for r in responses}) == 1
    page = responses[0]
    assert set(page.headers) == {"content-type", "content-length", "cache-control"}
    assert "orb" not in page.text.lower()
    assert (await client.get_json("/api/access"))["password_set"] is True


async def test_sign_in_from_any_address_lands_on_the_app(client, browser):
    await client.put_checked("/api/access/password", json={"password": "hunter2"})
    phone = browser()

    wrong = await _sign_in(phone, "hunter3")
    assert wrong.status_code == 401
    assert "Wrong password." in wrong.text

    right = await _sign_in(phone, "hunter2", "/api/settings")
    assert right.status_code == 303
    assert right.headers["location"] == "/"
    assert "httponly" in right.headers["set-cookie"].lower()
    assert (await phone.get("/api/settings")).status_code == 200


async def test_new_password_signs_out_every_other_browser(client, browser):
    await client.put_checked("/api/access/password", json={"password": "first"})
    phone = browser()
    await _sign_in(phone, "first")
    assert (await phone.get("/api/settings")).status_code == 200

    await client.put_checked("/api/access/password", json={"password": "second"})

    assert (await client.get("/api/settings")).status_code == 200
    assert (await phone.get("/api/settings")).status_code == 401
    assert (await _sign_in(phone, "first")).status_code == 401
    assert (await _sign_in(phone, "second")).status_code == 303


async def test_an_empty_password_opens_the_app(client, browser):
    await client.put_checked("/api/access/password", json={"password": "hunter2"})
    await client.put_checked("/api/access/password", json={"password": ""})

    assert (await browser().get("/api/settings")).status_code == 200
    assert (await client.get_json("/api/access"))["password_set"] is False


async def test_repeated_wrong_passwords_lock_out_that_address_only(client, browser):
    await client.put_checked("/api/access/password", json={"password": "hunter2"})
    guesser, owner = browser(), browser()

    for guess in ("a", "b", "c", "d", "e"):
        assert "Wrong password." in (await _sign_in(guesser, guess)).text
    refused = await _sign_in(guesser, "hunter2")
    assert refused.status_code == 401
    assert "Too many attempts" in refused.text

    assert (await _sign_in(owner, "hunter2")).status_code == 303


async def test_presets_never_carry_the_password_and_restore_keeps_the_live_one(client, db, db_path):
    from backend.features.presets import ALL_DOMAINS

    await client.put_checked("/api/access/password", json={"password": "old"})
    export = await client.post_json("/api/presets/export", json={"domains": list(ALL_DOMAINS), "strip_keys": False})
    with closing(sqlite3.connect(str(db_path.parent / "snapshots" / export["name"]))) as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM access_password").fetchone()[0] == 0

    await client.put_checked("/api/access/password", json={"password": "new"})
    live_before = await db.one("SELECT password_hash, session_key FROM access_password")
    await client.post_checked(f"/api/presets/{export['name']}/restore", json={})

    assert tuple(await db.one("SELECT password_hash, session_key FROM access_password")) == tuple(live_before)
