"""Character-expression upload/storage/serve routes + the classify-emotion 503.

The GGUF classifier is never present in CI, so classify-emotion 503s and only the
upload/storage/serve path is exercised end to end (no model needed)."""

import io
import zipfile


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _make_char(client) -> str:
    return await client.create("/api/characters", json={"name": "Expressor"})


async def test_upload_list_get_delete_roundtrip(client):
    card_id = await _make_char(client)
    zip_bytes = _zip({"joy.png": b"joybytes", "sub/anger.png": b"angerbytes", "skip.txt": b"x"})

    up = await client.post_json(
        f"/api/characters/{card_id}/expressions", files={"file": ("pack.zip", zip_bytes, "application/zip")}
    )
    assert up["labels"] == ["anger", "joy"]

    assert (await client.get(f"/api/characters/{card_id}/expressions")).json()["labels"] == ["anger", "joy"]

    img = await client.get_checked(f"/api/characters/{card_id}/expressions/joy")
    assert img.content == b"joybytes"
    etag = img.headers["etag"]

    await client.get_checked(f"/api/characters/{card_id}/expressions/joy", headers={"if-none-match": etag}, expected_status=304)

    assert (await client.get(f"/api/characters/{card_id}/expressions/fear")).status_code == 404

    assert (await client.delete(f"/api/characters/{card_id}/expressions")).status_code == 200
    assert (await client.get_json(f"/api/characters/{card_id}/expressions"))["labels"] == []


async def test_upload_replaces_previous_set(client):
    card_id = await _make_char(client)
    await client.post(
        f"/api/characters/{card_id}/expressions", files={"file": ("a.zip", _zip({"joy.png": b"1"}), "application/zip")}
    )
    await client.post(
        f"/api/characters/{card_id}/expressions", files={"file": ("b.zip", _zip({"anger.png": b"2"}), "application/zip")}
    )
    assert (await client.get_json(f"/api/characters/{card_id}/expressions"))["labels"] == ["anger"]


async def test_upload_no_matches_400(client):
    card_id = await _make_char(client)
    await client.post_checked(
        f"/api/characters/{card_id}/expressions",
        files={"file": ("x.zip", _zip({"notalabel.png": b"x"}), "application/zip")},
        expected_status=400,
    )


async def test_upload_missing_card_404(client):
    await client.post_checked(
        "/api/characters/nope/expressions",
        files={"file": ("x.zip", _zip({"joy.png": b"x"}), "application/zip")},
        expected_status=404,
    )


async def test_classify_emotion_503_when_deps_absent(client, monkeypatch):
    from backend.inference.local_models import dependencies

    monkeypatch.setattr(dependencies, "deps_ok", lambda feature=None: (False, "extras not installed"))
    await client.post_checked("/api/local-ml/classify-emotion", json={"text": "I am so happy!"}, expected_status=503)
