#!/usr/bin/env python3
"""Backend layering guardrail — the import direction AGENTS.md describes.

The layer stack is a convention, and until now nothing enforced it: Ruff and
Pyright are both perfectly happy with ``inference/`` reaching up into
``features/``. This parses every backend module's imports, resolves the
relative ones, and fails on an edge that points the wrong way.

Two rules:

  1. **Rank order.** Every top-level backend package has a rank; a module may
     import its own rank or lower. ``database`` may import ``core``; ``api``
     may import anything; ``inference`` may import neither ``features`` nor
     ``pipeline``.
  2. **Slices never import peers.** ``features/<a>`` may not import
     ``features/<b>``. A slice is self-contained by definition — a peer edge is
     how two features quietly become one.

DO NOT SPELL THIS AS A GREP. ``inference/local_models/llama_server/binary.py``
contains the literal ``https://api.github.com/repos/...``, so a grep for
``api\\.`` under ``inference/`` reports a violation that is not one and trains
the next person to ignore the check. This parses imports.

Exit non-zero on any violation. Wired into scripts/lint.sh.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# Lower number = lower layer. A module may import its own rank or lower.
# `analysis` and `inference` are peers by design: neither imports the other.
RANKS = {
    "core": 0,
    "database": 1,
    "analysis": 2,
    "inference": 2,
    "workflows": 3,
    "features": 4,
    "pipeline": 5,
    "api": 6,
}
#: backend/main.py and backend/__init__.py are the composition root; they sit
#: above everything and are ranked accordingly.
ROOT_RANK = max(RANKS.values())


def _module_parts(path: Path) -> list[str]:
    """``backend/api/routes/local_ml.py`` -> ``['backend', 'api', 'routes', 'local_ml']``."""
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _exists(parts: list[str]) -> bool:
    """Whether *parts* names a real module or package under the repo."""
    base = ROOT.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _package_parts(path: Path) -> list[str]:
    """The package a file lives in — the base a relative import counts up from.

    The same for ``cards/parsing.py`` and ``cards/__init__.py``: an
    ``__init__`` IS its package, so deriving this from the module path would
    count one level too many and report every intra-slice import as a peer edge.
    """
    return list(path.relative_to(ROOT).parent.parts)


def _targets(node: ast.AST, package: list[str]) -> list[list[str]]:
    """Every backend module *node* imports, as absolute part lists.

    A ``from .. import database`` resolves to the package ``backend``, and the
    thing actually imported is the name beside it — resolved here rather than
    left as a root edge, which would otherwise read as "imports all of
    backend" and report phantom violations.
    """
    out: list[list[str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.append(alias.name.split("."))
        return out
    if not isinstance(node, ast.ImportFrom):
        return out
    if node.level == 0:
        base = (node.module or "").split(".")
    else:
        # level 1 is the containing package, level 2 its parent, and so on.
        base = package[: len(package) - (node.level - 1)]
        if node.module:
            base = [*base, *node.module.split(".")]
    if not base:
        return out
    out.append(base)
    for alias in node.names:  # `from .. import database` — the name is the module
        candidate = [*base, alias.name]
        if _exists(candidate) and candidate not in out:
            out.append(candidate)
    return out


def _slice_of(parts: list[str]) -> tuple[str, str] | None:
    """``('features', 'cards')`` for a backend module, or ``None`` for anything else."""
    if len(parts) < 2 or parts[0] != "backend":
        return None
    return parts[1], (parts[2] if len(parts) > 2 else "")


def check() -> list[str]:
    problems: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = _module_parts(path)
        package = _package_parts(path)
        own_layer, own_slice = _slice_of(parts) or ("", "")
        own_rank = RANKS.get(own_layer, ROOT_RANK)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a file that will not parse is its own failure
            problems.append(f"{path.relative_to(ROOT)}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            where = f"{path.relative_to(ROOT)}:{node.lineno}"
            # One import statement resolves to both the package and the name
            # beside it (`from ..features import cards`), which is the same
            # edge said twice; report each layer and each peer slice once.
            edges = {e for t in _targets(node, package) if (e := _slice_of(t)) and e[0] in RANKS}
            for layer in sorted({layer for layer, _ in edges if RANKS[layer] > own_rank}):
                problems.append(f"{where}: {own_layer or 'backend'} imports upward into {layer}/")
            if own_layer == "features":
                peers = {s for layer, s in edges if layer == "features" and s and s != own_slice}
                for peer in sorted(peers):
                    problems.append(f"{where}: feature slice {own_slice!r} imports peer slice {peer!r}")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("Backend layer violations:\n  - " + "\n  - ".join(problems))
        return 1
    print(f"Backend layers OK ({len(RANKS)} ranked packages).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
