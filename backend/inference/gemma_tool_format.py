"""Parse Gemma's native tool-call format."""

from __future__ import annotations

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
