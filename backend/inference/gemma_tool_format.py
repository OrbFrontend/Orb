"""Parse Gemma's native tool-call format."""

from __future__ import annotations

import re
from typing import Any

OPEN = "<|tool_call>"
CLOSE = "<tool_call|>"
# One special token, used as both the opening and closing string delimiter.
QUOTE = '<|"|>'


def parse_gemma_tool_calls(content: str) -> list[dict]:
    """Return ``[{"name", "arguments"}, ...]`` for every native call in *content*."""
    out: list[dict] = []
    pos = 0
    while True:
        o = content.find(OPEN, pos)
        if o == -1:
            break
        c = content.find(CLOSE, o + len(OPEN))
        if c == -1:
            break  # no closing tag: the call was truncated mid-stream
        span = content[o + len(OPEN) : c]
        pos = c + len(CLOSE)
        call = _parse_call(span)
        if call is not None:
            out.append(call)
    return out


_NAME = re.compile(r"[A-Za-z_][\w.-]*")


def parse_gemma_call_strict(text: str) -> dict | None:
    """The one complete native call that *text* is, with only whitespace around it, or ``None``.

    Unlike :func:`parse_gemma_tool_calls`, which reads what it can, the body must follow the grammar whole: every segment is
    ``key:value`` with a key named once, and every value is a closed quoted string, a number, ``true``, ``false``, ``null``, or a
    bracketed array or object of the same. A call cut off mid-way, an unquoted word, text after a closing quote or a segment
    without a colon is refused rather than read as a shorter call.
    """
    text = text.strip()
    if not (text.startswith(OPEN) and text.endswith(CLOSE)):
        return None
    span = text[len(OPEN) : -len(CLOSE)].strip()
    if OPEN in span or CLOSE in span or not span.startswith("call:"):
        return None
    brace = span.find("{")
    name = span[len("call:") : brace].strip() if brace != -1 else ""
    if not _NAME.fullmatch(name):
        return None
    reader = _StrictReader(span, brace)
    try:
        arguments = reader.object()
        reader.space()
    except _Malformed:
        return None
    return {"name": name, "arguments": arguments} if reader.at_end() else None


class _Malformed(Exception):
    """The text breaks the strict call grammar."""


_LITERAL = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")


class _StrictReader:
    """A reader that consumes the native grammar exactly, refusing anything it cannot account for character by character."""

    def __init__(self, text: str, start: int):
        self.text = text
        self.i = start

    def at_end(self) -> bool:
        return self.i == len(self.text)

    def space(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def expect(self, char: str) -> None:
        if not self.text.startswith(char, self.i):
            raise _Malformed
        self.i += len(char)

    def object(self) -> dict[str, Any]:
        self.expect("{")
        out: dict[str, Any] = {}
        self.space()
        if self.text.startswith("}", self.i):
            self.i += 1
            return out
        while True:
            self.space()
            key = _NAME.match(self.text, self.i)
            if key is None or key.group() in out:
                raise _Malformed
            self.i = key.end()
            self.space()
            self.expect(":")
            out[key.group()] = self.value()
            self.space()
            if self.text.startswith(",", self.i):
                self.i += 1
                continue
            self.expect("}")
            return out

    def array(self) -> list[Any]:
        self.expect("[")
        out: list[Any] = []
        self.space()
        if self.text.startswith("]", self.i):
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self.space()
            if self.text.startswith(",", self.i):
                self.i += 1
                continue
            self.expect("]")
            return out

    def value(self) -> Any:
        self.space()
        if self.text.startswith(QUOTE, self.i):
            start = self.i + len(QUOTE)
            end = self.text.find(QUOTE, start)
            if end == -1:
                raise _Malformed
            self.i = end + len(QUOTE)
            return self.text[start:end]
        if self.text.startswith("{", self.i):
            return self.object()
        if self.text.startswith("[", self.i):
            return self.array()
        literal = _LITERAL.match(self.text, self.i)
        if literal is None:
            raise _Malformed
        self.i = literal.end()
        word = literal.group()
        if word in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[word]
        return float(word) if any(mark in word for mark in ".eE") else int(word)


def _parse_call(span: str) -> dict | None:
    """Parse one ``call:NAME{BODY}`` span; None if it is not a call."""
    span = span.strip()
    if not span.startswith("call:"):
        return None
    brace = span.find("{")
    if brace == -1:
        return None
    name = span[len("call:") : brace].strip()
    if not name:
        return None
    end = _match(span, brace, "{", "}")
    body = span[brace + 1 : end] if end != -1 else span[brace + 1 :]
    return {"name": name, "arguments": _parse_body(body)}


def _parse_body(body: str) -> dict:
    """Parse a ``key:value,...`` body into an arguments dict."""
    args: dict = {}
    for seg in _split_top(body, ","):
        if not seg.strip():
            continue
        key, raw = _split_kv(seg)
        if key:
            args[key] = _parse_value(raw)
    return args


def _parse_value(s: str) -> Any:
    s = s.strip()
    if not s:
        return ""
    if s.startswith("["):
        return _parse_array(s)
    if s.startswith("{"):
        return _parse_object(s)
    if s.startswith(QUOTE):
        return _parse_string(s)
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_array(s: str) -> list:
    end = _match(s, 0, "[", "]")
    inner = s[1:end] if end != -1 else s[1:]
    if not inner.strip():
        return []
    return [_parse_value(e) for e in _split_top(inner, ",")]


def _parse_object(s: str) -> dict:
    # An object shares the call body's key:value grammar.
    end = _match(s, 0, "{", "}")
    inner = s[1:end] if end != -1 else s[1:]
    return _parse_body(inner)


def _parse_string(s: str) -> str:
    rest = s[len(QUOTE) :]
    end = rest.find(QUOTE)
    return rest[:end] if end != -1 else rest


def _match(s: str, open_idx: int, oc: str, cc: str) -> int:
    """String-aware index of the bracket closing the one at *open_idx*; -1 if none."""
    i, in_str, depth = open_idx, False, 0
    n = len(s)
    while i < n:
        if s.startswith(QUOTE, i):
            in_str = not in_str
            i += len(QUOTE)
            continue
        ch = s[i]
        if not in_str:
            if ch == oc:
                depth += 1
            elif ch == cc:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _split_top(s: str, sep: str) -> list[str]:
    """Split on *sep*, ignoring it inside ``<|"|>`` strings and inside ``[]`` / ``{}``."""
    parts: list[str] = []
    buf: list[str] = []
    i, in_str, brk, brc = 0, False, 0, 0
    n = len(s)
    while i < n:
        if s.startswith(QUOTE, i):
            in_str = not in_str
            buf.append(QUOTE)
            i += len(QUOTE)
            continue
        ch = s[i]
        if not in_str:
            if ch == "[":
                brk += 1
            elif ch == "]":
                brk -= 1
            elif ch == "{":
                brc += 1
            elif ch == "}":
                brc -= 1
            elif ch == sep and brk == 0 and brc == 0:
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _split_kv(seg: str) -> tuple[str, str]:
    """Split *seg* on its first top-level ``:``; ``("", "")`` if there is none."""
    i, in_str, brk, brc = 0, False, 0, 0
    n = len(seg)
    while i < n:
        if seg.startswith(QUOTE, i):
            in_str = not in_str
            i += len(QUOTE)
            continue
        ch = seg[i]
        if not in_str:
            if ch == "[":
                brk += 1
            elif ch == "]":
                brk -= 1
            elif ch == "{":
                brc += 1
            elif ch == "}":
                brc -= 1
            elif ch == ":" and brk == 0 and brc == 0:
                return seg[:i].strip(), seg[i + 1 :]
        i += 1
    return "", ""
