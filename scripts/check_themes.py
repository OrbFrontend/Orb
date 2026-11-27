#!/usr/bin/env python3
"""Reject theme selectors the app no longer has, and `ui-*` roles listed beside their own members."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
THEMES = FE / "themes"

TOKEN = re.compile(r"(?<![\w-])([#.])(-?[A-Za-z_][\w-]*)")
CLASS_ATTR = re.compile(r"""\bclass(?:Name)?\s*=\s*["'`]([^"'`]*)""")
ID_ATTR = re.compile(r"""\bid=["']([\w-]+)""")


def app_sources() -> list[Path]:
    files = [*FE.rglob("*.js"), *FE.rglob("*.html")]
    return [p for p in files if not {"vendor", "themes"} & set(p.relative_to(FE).parts)]


def role_members(sources: list[Path]) -> dict[str, set[str]]:
    strings: list[tuple[set[str], str | None]] = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for m in CLASS_ATTR.finditer(text):
            classes = set(re.findall(r"[A-Za-z_][\w-]*", re.sub(r"\$\{[^}]*\}?", " ", m.group(1))))
            start, end = text.rfind("<", 0, m.start()), text.find(">", m.end())
            tag = text[start:end] if start != -1 and end != -1 and "\n" not in text[start : m.start()] else ""
            id_match = ID_ATTR.search(tag)
            strings.append((classes, id_match.group(1) if id_match else None))
    members: dict[str, set[str]] = {}
    seen_with: dict[str, list[set[str]]] = {}
    for classes, element_id in strings:
        for role in (c for c in classes if c.startswith("ui-")):
            if element_id:
                members.setdefault("." + role, set()).add("#" + element_id)
        for c in classes:
            seen_with.setdefault(c, []).append(classes)
    for c, groups in seen_with.items():
        if c.startswith("ui-"):
            continue
        for role in set.intersection(*groups):
            if role.startswith("ui-"):
                members.setdefault("." + role, set()).add("." + c)
    return members


def selector_lists(text: str):
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    start = 0
    for i, ch in enumerate(text):
        if ch in "};":
            start = i + 1
        elif ch == "{":
            prelude = text[start:i]
            if not prelude.strip().startswith("@"):
                yield start, prelude
            start = i + 1


def split_list(prelude: str):
    depth, begin = 0, 0
    for i, ch in enumerate(prelude + ","):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            yield begin, prelude[begin:i]
            begin = i + 1


def subject_tokens(part: str) -> set[str]:
    bare = re.sub(r"\[[^\]]*\]", "", part).strip()
    depth, last = 0, 0
    for i, ch in enumerate(bare):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in " >+~":
            last = i + 1
    return {kind + name for kind, name in TOKEN.findall(re.sub(r"\([^()]*\)", "", bare[last:]))}


def main() -> int:
    sources = app_sources()
    known = set()
    for path in sources:
        known.update(re.findall(r"[A-Za-z_][\w-]*", path.read_text(encoding="utf-8")))
    members = role_members(sources)
    themes = sorted(THEMES.glob("*.css"))
    errors: list[str] = []
    for theme in themes:
        text = theme.read_text(encoding="utf-8")
        for offset, prelude in selector_lists(text):
            parts = list(split_list(prelude))
            subjects = [subject_tokens(part) for _, part in parts]
            roles = set().union(*subjects) & members.keys()
            for (at, part), subject in zip(parts, subjects, strict=True):
                line = text.count("\n", 0, offset + at + len(part) - len(part.lstrip())) + 1
                where = f"{theme.relative_to(ROOT)}:{line}"
                for kind, name in TOKEN.findall(re.sub(r"\[[^\]]*\]", "", part)):
                    if name not in known:
                        errors.append(f"{where}: {kind}{name} is not in the app; drop it from the selector")
                for role in sorted(roles - subject):
                    for member in sorted(subject & members[role]):
                        errors.append(f"{where}: {member} carries {role}; style {role} instead")
    print(f"theme check: {len(themes)} themes, {len(members)} roles")
    if errors:
        print("\nTHEME CHECK FAILED:")
        for e in dict.fromkeys(errors):
            print(f"  {e}")
        return 1
    print("theme check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
