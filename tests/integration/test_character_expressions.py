"""Character-expression upload/storage/serve routes + the classify-emotion 503.

The GGUF classifier is never present in CI, so classify-emotion 503s and only the
upload/storage/serve path is exercised end to end (no model needed)."""

import base64
import io
import zipfile

import pytest

from backend.database import set_character_expressions


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


@pytest.mark.parametrize("kind", ["expression", "avatar", "thumbnail", "persona"])
@pytest.mark.parametrize("mime", ["image/svg+xml", "text/html"])
async def test_stored_image_content_is_sandboxed_on_navigation_and_revalidation(client, kind, mime):
    # Legacy/imported bytes may be an active document, even when normally used as an <img>.
    content = b'<svg xmlns="http://www.w3.org/2000/svg"><script>window.probe=1</script></svg>'
    avatar = {"avatar_b64": base64.b64encode(content).decode(), "avatar_mime": mime}
    if kind == "persona":
        row_id = await client.create("/api/user-personas", json={"name": "Probe", **avatar})
        path = f"/api/user-personas/{row_id}/avatar"
    else:
        row_id = await client.create("/api/characters", json={"name": "Probe", **avatar})
        if kind == "expression":
            await set_character_expressions(row_id, {"neutral": (avatar["avatar_b64"], mime)})
            path = f"/api/characters/{row_id}/expressions/neutral"
        else:
            path = f"/api/characters/{row_id}/avatar" + ("/thumb" if kind == "thumbnail" else "")

    response = await client.get_checked(path)
    assert response.content == content
    cached = await client.get_checked(path, headers={"if-none-match": response.headers["etag"]}, expected_status=304)
    for result in (response, cached):
        assert result.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert result.headers["x-content-type-options"] == "nosniff"
