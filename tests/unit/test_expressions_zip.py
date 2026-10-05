"""extract_expressions_zip: label/ext filtering + zip-bomb guards;
_expression_pack: pulling a card's embedded chub expression pack."""

from __future__ import annotations

import base64
import io
import zipfile

import httpx
import pytest

from backend.features.cards import expressions
from backend.features.cards.expressions import _expression_pack, extract_expressions_zip


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_filters_to_known_labels_and_flattens_paths():
    out = extract_expressions_zip(
        _zip(
            {
                "Wendy Hollis/joy.png": b"joybytes",  # nested -> basename
                "ADMIRATION.PNG": b"admirebytes",  # case-insensitive label + ext
                "readme.txt": b"nope",  # wrong ext
                "notalabel.png": b"nope",  # not a go-emotions label
            }
        )
    )
    assert set(out) == {"joy", "admiration"}
    assert out["joy"] == (base64.b64encode(b"joybytes").decode(), "image/png")


def test_rejects_too_many_entries():
    with pytest.raises(ValueError, match="too many entries"):
        extract_expressions_zip(_zip({f"joy{i}.png": b"x" for i in range(201)}))


def test_rejects_oversized_entry():
    with pytest.raises(ValueError, match="exceeds 5 MB"):
        extract_expressions_zip(_zip({"joy.png": b"x" * (5 * 1024 * 1024 + 1)}))


def test_expression_pack_selection():
    pack = {"compressed": "https://x/e.zip", "expressions": {"joy": "https://x/joy.png"}}
    assert _expression_pack({"extensions": {"chub": {"expressions": pack}}}) == pack
    # chub carries a null pack on the CDN card PNG; manual cards carry nothing.
    assert _expression_pack({"extensions": {"chub": {"expressions": None}}}) is None
    assert _expression_pack({"extensions": {}}) is None
    assert _expression_pack({}) is None


def test_rejects_aggregate_decompression_before_reading_entries(monkeypatch):
    monkeypatch.setattr(expressions, "_MAX_PACK", 8)
    with pytest.raises(ValueError, match="uncompressed"):
        extract_expressions_zip(_zip({"joy.png": b"12345", "anger.png": b"12345"}))


async def test_expression_download_stops_at_decoded_size_limit_and_closes_stream():
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.consumed += 1
                yield b"x" * (64 * 1024)

        async def aclose(self):
            self.closed = True

    body = Body()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await expressions._fetch_limited(client, "https://pack.test/joy.png", 64 * 1024) is None
    assert body.consumed == 2
    assert body.closed


async def test_oversized_zip_download_falls_back_to_individual_images(monkeypatch):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(expressions, "_MAX_PACK", 8)

    def response(request):
        if request.url.path.endswith(".zip"):
            return httpx.Response(200, content=b"x" * 9)
        return httpx.Response(200, content=b"joy", headers={"content-type": "image/webp; charset=binary"})

    transport = httpx.MockTransport(response)
    monkeypatch.setattr(expressions.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    card = {
        "extensions": {
            "chub": {
                "expressions": {"compressed": "https://pack.test/e.zip", "expressions": {"joy": "https://pack.test/joy.webp"}}
            }
        }
    }
    assert await expressions.fetch_embedded_expressions(card) == {"joy": (base64.b64encode(b"joy").decode(), "image/webp")}
