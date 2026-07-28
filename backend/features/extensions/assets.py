"""What a package file may *be*, beyond being well-formed.

A manifest may reference JSON, safe raster/audio/video assets, and plain text
(README / LICENSE). Everything a browser would execute or interpret with its
own parser is rejected in v1: HTML, JavaScript, CSS, SVG, PDF, and browser
fonts. SVG is on that list despite being an image -- it carries script and
external references, so "it renders in an <img>" is not the same claim as "it
is inert".

The check is two-sided on purpose. The declared extension decides which media
type a path is *allowed* to be, and the leading bytes have to agree. Extension
alone would let ``evil.png`` be a HTML file that a sloppy downstream consumer
sniffs; bytes alone would let a package ship an asset whose type the manifest
author never declared, and the asset route serves what the compiler recorded.
A file that passes both is served with ``X-Content-Type-Options: nosniff`` and
the recorded type, so there is no third opinion to disagree with.
"""

from __future__ import annotations

import posixpath

from .errors import PackageValidationError

TEXT_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt"})
"""Extensions served as ``text/plain``. README and LICENSE bodies, nothing more."""

BINARY_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

REJECTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".js",
        ".mjs",
        ".cjs",
        ".css",
        ".svg",
        ".pdf",
        ".wasm",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
    }
)
"""Named individually rather than left to fall through the allowlist.

Falling through produces "unsupported asset type '.svg'", which reads like a
gap someone should fill. These are refusals, so they get a refusal message.
"""

# Leading-byte signatures, checked against the extension's declared type. Some
# containers (RIFF, ISO-BMFF) carry the real subtype a few bytes in, so the
# predicate is a callable rather than a prefix table.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "audio/mpeg": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    "audio/ogg": (b"OggS",),
}


def _riff_matches(data: bytes, fourcc: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == fourcc


def _isobmff(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


ARTIFACT_MEDIA_TYPES: frozenset[str] = frozenset(BINARY_MEDIA_TYPES.values()) | {
    "text/plain",
    "application/json",
}
"""What ``artifact.emit`` may declare a produced file to be.

The asset allowlist plus the two text shapes a flow can produce on its own. An
artifact's mime is stored and travels to the frontend, which turns it into a
blob the user can open -- so ``text/html`` or ``image/svg+xml`` here would be
the same active-content hole the package-asset allowlist exists to close, one
step further downstream. The parameters (``; charset=...``) are dropped before
the check, because a charset is not a type."""


def normalize_artifact_mime(raw: str) -> str:
    """Lowercase a declared artifact mime and drop its parameters."""
    return raw.split(";", 1)[0].strip().lower()


def asset_media_type(path: str) -> str:
    """The media type *path*'s extension declares, or raise.

    Case-insensitive on the extension only: package paths are case-sensitive
    (see :mod:`.paths`), but ``icon.PNG`` and ``icon.png`` are the same *kind*
    of file, and rejecting one for its capitalization would be a rule about
    typing rather than about safety.
    """
    ext = posixpath.splitext(path)[1].lower()
    if ext in REJECTED_EXTENSIONS:
        raise PackageValidationError(
            f"asset {path!r} uses the active format {ext!r}; v1 packages may reference only JSON, "
            f"plain text, and inert raster/audio/video assets"
        )
    if ext in TEXT_EXTENSIONS:
        return "text/plain; charset=utf-8"
    media = BINARY_MEDIA_TYPES.get(ext)
    if media is None:
        raise PackageValidationError(
            f"asset {path!r} has unsupported extension {ext!r}; allowed: "
            f"{', '.join(sorted(BINARY_MEDIA_TYPES) + sorted(TEXT_EXTENSIONS))}"
        )
    return media


def assert_bytes_match(path: str, media_type: str, data: bytes) -> None:
    """Reject an asset whose leading bytes contradict its declared type."""
    if media_type.startswith("text/"):
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise PackageValidationError(f"asset {path!r} is declared text but is not valid UTF-8") from None
        return
    signatures = _SIGNATURES.get(media_type)
    if signatures is not None:
        if not any(data.startswith(sig) for sig in signatures):
            raise PackageValidationError(f"asset {path!r} does not contain {media_type} data")
        return
    if media_type == "image/webp":
        ok = _riff_matches(data, b"WEBP")
    elif media_type == "audio/wav":
        ok = _riff_matches(data, b"WAVE")
    elif media_type in ("audio/mp4", "video/mp4"):
        ok = _isobmff(data)
    elif media_type == "video/webm":
        ok = data.startswith(b"\x1a\x45\xdf\xa3")
    else:  # pragma: no cover -- unreachable while the table above is closed
        raise PackageValidationError(f"asset {path!r} has no signature check for {media_type}")
    if not ok:
        raise PackageValidationError(f"asset {path!r} does not contain {media_type} data")
