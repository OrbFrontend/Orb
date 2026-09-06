#!/usr/bin/env python3
"""Backend layering guardrail — the import graph AGENTS.md describes.

This parses every backend module's imports, resolves relative imports, and
fails on an edge that is absent from the explicit allowed-edge matrix.

Three rules:

  1. **Explicit edges.** Each top-level Python package has a complete set of
     backend packages it may import. Same-package imports are always allowed.
  2. **Every layer is classified.** A new Python-bearing top-level package or
     module must be classified rather than silently becoming a composition
     root.
  3. **Slices never import peers.** ``features/<a>`` may not import
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

# The source of truth for cross-package imports. Same-package imports are
# allowed implicitly. Features and workflows deliberately remain siblings.
ALLOWED_EDGES: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "database": frozenset({"core"}),
    "inference": frozenset({"core"}),
    "prompting": frozenset({"core"}),
    "analysis": frozenset({"database", "core"}),
    "workflows": frozenset({"prompting", "inference", "analysis", "database", "core"}),
    "features": frozenset({"prompting", "inference", "analysis", "database", "core"}),
    "pipeline": frozenset({"features", "workflows", "prompting", "inference", "analysis", "database", "core"}),
    "api": frozenset({"pipeline", "features", "workflows", "prompting", "inference", "analysis", "database", "core"}),
}
ROOT_ALLOWED = frozenset(ALLOWED_EDGES)
ROOT_LAYER = "root"


def _module_parts(path: Path, *, root: Path = ROOT) -> list[str]:
    """``backend/api/routes/local_ml.py`` -> ``['backend', 'api', 'routes', 'local_ml']``."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _exists(parts: list[str], *, root: Path = ROOT) -> bool:
    """Whether *parts* names a real module or package under the repo."""
    base = root.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _package_parts(path: Path, *, root: Path = ROOT) -> list[str]:
    """The package a file lives in — the base a relative import counts up from.

    The same for ``cards/parsing.py`` and ``cards/__init__.py``: an
    ``__init__`` IS its package, so deriving this from the module path would
    count one level too many and report every intra-slice import as a peer edge.
    """
    return list(path.relative_to(root).parent.parts)


def _targets(node: ast.AST, package: list[str], *, root: Path = ROOT) -> list[list[str]]:
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
        if _exists(candidate, root=root) and candidate not in out:
            out.append(candidate)
    return out


def _slice_of(parts: list[str]) -> tuple[str, str] | None:
    """``('features', 'cards')`` for a backend module, or ``None`` for anything else."""
    if len(parts) < 2 or parts[0] != "backend":
        return None
    if len(parts) == 2 and parts[1] == "main":
        return ROOT_LAYER, ""
    return parts[1], (parts[2] if len(parts) > 2 else "")


def _python_packages(backend: Path) -> set[str]:
    return {
        path.name
        for path in backend.iterdir()
        if path.is_dir() and any("__pycache__" not in module.parts for module in path.rglob("*.py"))
    }


def _unclassified_top_level_modules(backend: Path) -> set[str]:
    """Root modules other than the two explicit composition-root modules."""
    return {
        path.name
        for path in backend.glob("*.py")
        if path.name not in {"__init__.py", "main.py"}
    }


def check(*, root: Path = ROOT, backend: Path | None = None) -> list[str]:
    backend = backend or root / "backend"
    problems: list[str] = []
    for package in sorted(_python_packages(backend) - ALLOWED_EDGES.keys()):
        problems.append(
            f"backend/{package}/: unclassified Python package (add it to ALLOWED_EDGES)"
        )
    for module in sorted(_unclassified_top_level_modules(backend)):
        problems.append(f"backend/{module}: unclassified top-level Python module")
    for path in sorted(backend.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = _module_parts(path, root=root)
        package = _package_parts(path, root=root)
        own_layer, own_slice = _slice_of(parts) or ("", "")
        allowed = (
            ROOT_ALLOWED
            if own_layer in ("", ROOT_LAYER)
            else ALLOWED_EDGES.get(own_layer, frozenset())
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a file that will not parse is its own failure
            problems.append(f"{path.relative_to(root)}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            where = f"{path.relative_to(root)}:{node.lineno}"
            # One import statement resolves to both the package and the name
            # beside it (`from ..features import cards`), which is the same
            # edge said twice; report each layer and each peer slice once.
            edges = {
                edge
                for target in _targets(node, package, root=root)
                if (edge := _slice_of(target))
                and edge[0] in {*ALLOWED_EDGES, ROOT_LAYER}
            }
            for layer in sorted({layer for layer, _ in edges if layer != own_layer and layer not in allowed}):
                problems.append(f"{where}: {own_layer or 'backend'} may not import {layer}")
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
    print(f"Backend layers OK ({len(ALLOWED_EDGES)} classified packages).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
