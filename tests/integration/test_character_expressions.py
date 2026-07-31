"""Character-expression upload/storage/serve routes + the classify-emotion 503.

The GGUF classifier is never present in CI, so classify-emotion 503s and only the
upload/storage/serve path is exercised end to end (no model needed)."""

from __future__ import annotations

import io
import zipfile


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _make_char(client) -> str:
    return (await client.post("/api/characters", json={"name": "Expressor"})).json()["id"]


async def test_upload_list_get_delete_roundtrip(client):
    card_id = await _make_char(client)
    zip_bytes = _zip({"joy.png": b"joybytes", "sub/anger.png": b"angerbytes", "skip.txt": b"x"})

    up = await client.post(
        f"/api/characters/{card_id}/expressions",
        files={"file": ("pack.zip", zip_bytes, "application/zip")},
    )
    assert up.status_code == 200
    assert up.json()["labels"] == ["anger", "joy"]

    listed = await client.get(f"/api/characters/{card_id}/expressions")
    assert listed.json()["labels"] == ["anger", "joy"]

    img = await client.get(f"/api/characters/{card_id}/expressions/joy")
    assert img.status_code == 200
    assert img.content == b"joybytes"
    etag = img.headers["etag"]

    not_mod = await client.get(f"/api/characters/{card_id}/expressions/joy", headers={"if-none-match": etag})
    assert not_mod.status_code == 304

    assert (await client.get(f"/api/characters/{card_id}/expressions/fear")).status_code == 404

    assert (await client.delete(f"/api/characters/{card_id}/expressions")).status_code == 200
    assert (await client.get(f"/api/characters/{card_id}/expressions")).json()["labels"] == []


async def test_upload_replaces_previous_set(client):
    card_id = await _make_char(client)
    await client.post(
        f"/api/characters/{card_id}/expressions",
        files={"file": ("a.zip", _zip({"joy.png": b"1"}), "application/zip")},
    )
    await client.post(
        f"/api/characters/{card_id}/expressions",
        files={"file": ("b.zip", _zip({"anger.png": b"2"}), "application/zip")},
    )
    assert (await client.get(f"/api/characters/{card_id}/expressions")).json()["labels"] == ["anger"]


async def test_upload_no_matches_400(client):
    card_id = await _make_char(client)
    resp = await client.post(
        f"/api/characters/{card_id}/expressions",
        files={"file": ("x.zip", _zip({"notalabel.png": b"x"}), "application/zip")},
    )
    assert resp.status_code == 400


async def test_upload_missing_card_404(client):
    resp = await client.post(
        "/api/characters/nope/expressions",
        files={"file": ("x.zip", _zip({"joy.png": b"x"}), "application/zip")},
    )
    assert resp.status_code == 404


async def test_classify_emotion_503_when_deps_absent(client, monkeypatch):
    from backend.inference import local_ml

    monkeypatch.setattr(local_ml, "deps_ok", lambda: (False, "extras not installed"))
    resp = await client.post("/api/local-ml/classify-emotion", json={"text": "I am so happy!"})
    assert resp.status_code == 503
