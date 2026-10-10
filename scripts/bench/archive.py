"""Gzip-by-suffix file access for bench data; mtime 0 keeps rewrites byte-identical."""

from __future__ import annotations

import gzip
from pathlib import Path


def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def read_text(path: Path) -> str:
    return read_bytes(path).decode()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = text.encode()
    temporary.write_bytes(gzip.compress(data, compresslevel=9, mtime=0) if path.suffix == ".gz" else data)
    temporary.replace(path)


def append_line(path: Path, line: str) -> None:
    if path.suffix == ".gz":
        with gzip.GzipFile(path, "ab", mtime=0) as stream:
            stream.write((line + "\n").encode())
    else:
        with path.open("a") as stream:
            stream.write(line + "\n")
