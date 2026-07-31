"""extract_expressions_zip: label/ext filtering + zip-bomb guards;
_expression_pack: pulling a card's embedded chub expression pack."""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

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
                "Ashley Leyley/joy.png": b"joybytes",  # nested → basename
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
