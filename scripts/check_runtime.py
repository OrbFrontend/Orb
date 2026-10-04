"""Probe required runtime imports without loading Orb, its database or models.

Used by both launchers after pip fails. This checks usability of the installed
packages, not their pinned versions, so an existing offline install can run.
ML packages in requirements-ml.txt deliberately do not participate.
"""

from __future__ import annotations

import importlib
import sys

# requirements.txt distributions and their import surfaces. Import Image as well
# as PIL to exercise Pillow's native extension; socksio covers httpx[socks].
RUNTIME_IMPORTS = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("aiosqlite", "aiosqlite"),
    ("httpx", "httpx"),
    ("httpx[socks]", "socksio"),
    ("pydantic", "pydantic"),
    ("Pillow", "PIL.Image"),
    ("python-multipart", "python_multipart"),
    ("edge-tts", "edge_tts"),
    ("regex", "regex"),
    ("jsonschema", "jsonschema"),
)


def runtime_dependency_errors() -> list[str]:
    """Return missing or broken required imports; never import application code."""
    errors = []
    for distribution, module in RUNTIME_IMPORTS:
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append(f"{distribution} ({module}): {exc}")
    return errors


def main() -> int:
    errors = runtime_dependency_errors()
    if errors:
        print("Required runtime dependencies are unavailable:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
