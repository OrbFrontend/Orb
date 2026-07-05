"""Local-ML routes: status tri-state, download gating, and the enable toggle.
No network — download() is only reached in the deps-missing 400 case."""

from __future__ import annotations

from backend.inference import local_ml


async def test_download_400_when_deps_missing(client, monkeypatch):
    monkeypatch.setattr(local_ml, "deps_ok", lambda: (False, "extras not installed"))
    # download() must never run; guard against an accidental network hit.
    monkeypatch.setattr(local_ml, "download", lambda f: (_ for _ in ()).throw(AssertionError("must not download")))
    resp = await client.post("/api/local-ml/autocomplete/download")
    assert resp.status_code == 400


async def test_download_unknown_feature_404(client):
    resp = await client.post("/api/local-ml/nope/download")
    assert resp.status_code == 404


async def test_enable_toggle_roundtrips(client):
    resp = await client.post("/api/local-ml/autocomplete/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["local_ml_enabled"] == {"autocomplete": False}
    # Status reflects the flip.
    st = (await client.get("/api/local-ml/status")).json()
    assert st["features"]["autocomplete"]["enabled"] is False
