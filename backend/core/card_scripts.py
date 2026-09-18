"""Read-time card projections. Never mutate a stored message or extension blob."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any, Literal, NamedTuple

import regex

logger = logging.getLogger(__name__)
MAX_SCRIPTS = 50
MAX_PATTERN_LENGTH = 4096
MAX_TEXT_LENGTH = 100_000
TIMEOUT_SECONDS = 0.05
JS_FLAGS = frozenset("gmixXsuUAJ")  # Accepted by the source engine's parser.
ENGINE_FLAGS = frozenset("gimsu")  # Supported by both projections.
_MATCH_MACRO = re.compile(r"\{\{match\}\}", re.I)
Channel = Literal["prompt", "display"]
Resolver = Callable[[str], str]


def card_render_options(extensions: Any) -> tuple[list[dict], str]:
    """Return enabled script declarations and the explicit card stylesheet."""
    if not isinstance(extensions, Mapping):
        return [], ""
    orb = extensions.get("orb")
    orb = orb if isinstance(orb, Mapping) else {}
    css = orb.get("display_css", "")
    raw = extensions.get("regex_scripts")
    scripts = []
    if orb.get("card_scripts_enabled") is not False and isinstance(raw, list):
        for script in raw[:MAX_SCRIPTS]:
            if not isinstance(script, dict) or script.get("disabled"):
                continue
            placement = script.get("placement")
            if not isinstance(placement, list) or not any(p in (1, 2) for p in placement):
                continue
            find, replacement = script.get("findRegex"), script.get("replaceString", "")
            if isinstance(find, str) and find and len(find) <= MAX_PATTERN_LENGTH and isinstance(replacement, str):
                scripts.append(script)
    return scripts, css if isinstance(css, str) else ""


def is_display_script(script: Mapping[str, Any]) -> bool:
    return bool(script.get("markdownOnly")) or not script.get("promptOnly")


def is_prompt_script(script: Mapping[str, Any]) -> bool:
    # Unflagged scripts affect both views in the source engine.
    return bool(script.get("promptOnly")) or not script.get("markdownOnly")


def _parse_literal(source: str) -> tuple[str, str] | None:
    """Split `/pattern/flags`, or return None when the whole string is the pattern."""
    if not source.startswith("/"):
        return None
    end = source.rfind("/")
    if end <= 0:
        return None
    flags = source[end + 1 :]
    # Preserve the source engine's distinction between flag-shaped and malformed.
    if flags and (len(set(flags)) != len(flags) or not set(flags) <= JS_FLAGS):
        return None
    return source[1:end], flags


def _replacement(template: str, match: Any, source: str, resolve: Resolver | None) -> str:
    """Expand JS replacement tokens without interpreting backslashes."""

    def replace(token: re.Match) -> str:
        key = token[0][1:]
        if key == "$":
            return "$"
        if key == "&":
            return match[0]
        if key == "`":
            return source[: match.start()]
        if key == "'":
            return source[match.end() :]
        if key.startswith("<"):
            try:
                return match[key[1:-1]] or ""
            except (IndexError, KeyError):
                return ""
        index = int(key)
        if index == 0:
            return match[0]
        if 0 < index <= match.re.groups:
            return match[index] or ""
        if len(key) == 2 and 0 < int(key[0]) <= match.re.groups:
            return (match[int(key[0])] or "") + key[1]
        return token[0]

    expanded = re.sub(r"\$(?:[$&`']|<[^>]*>|[0-9]{1,2})", replace, _MATCH_MACRO.sub("$0", template))
    return resolve(expanded) if resolve and "{{" in expanded else expanded


class _Script(NamedTuple):
    pattern: Any
    replacement: str
    placement: tuple[int, ...]
    prompt: bool
    display: bool
    global_replace: bool


class CardScripts(NamedTuple):
    scripts: tuple[_Script, ...] = ()

    @classmethod
    def from_extensions(cls, extensions: Any) -> CardScripts:
        declarations, _ = card_render_options(extensions)
        compiled = []
        for script in declarations:
            source, flags = _parse_literal(script["findRegex"]) or (script["findRegex"], "")
            try:
                if not set(flags) <= ENGINE_FLAGS:
                    raise ValueError("unsupported regex flags")
                options = regex.VERSION0
                for flag, option in (("i", regex.I), ("m", regex.M), ("s", regex.S)):
                    if flag in flags:
                        options |= option
                compiled.append(
                    _Script(
                        regex.compile(source, options),
                        script.get("replaceString", ""),
                        tuple(p for p in script["placement"] if p in (1, 2)),
                        is_prompt_script(script),
                        is_display_script(script),
                        "g" in flags,
                    )
                )
            except (regex.error, ValueError, RecursionError):
                logger.warning("Ignoring invalid or unsupported card regex")
        return cls(tuple(compiled))

    def apply(self, text: str, channel: Channel, role: str, resolve: Resolver | None = None) -> str:
        """Apply matching scripts to one message."""
        placement = {"user": 1, "assistant": 2}.get(role)
        if not placement or not self.scripts or len(text) > MAX_TEXT_LENGTH:
            return text
        original = text
        deadline = monotonic() + TIMEOUT_SECONDS
        try:
            for script in self.scripts:
                if placement not in script.placement or not getattr(script, channel):
                    continue
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                source = text
                text = script.pattern.sub(
                    lambda match, replacement=script.replacement, source=source: _replacement(
                        replacement, match, source, resolve
                    ),
                    text,
                    count=0 if script.global_replace else 1,
                    timeout=remaining,
                )
                if len(text) > MAX_TEXT_LENGTH:
                    logger.warning("Card regex output exceeded message limit; using original text")
                    return original
        except TimeoutError:
            logger.warning("Card regex timed out; using original text")
            return original
        return text
