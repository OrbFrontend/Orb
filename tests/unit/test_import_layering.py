"""Static guard for the backend's one-way layered architecture.

The dependency direction is strictly downward (see ``agents-md-analyze-the-file``
and each layer's ``__init__`` docstring):

    api -> {pipeline, features} -> workflows -> {inference, analysis} -> core
                                                      \\-> database -> core

A layer may import only from the layers below it (same-layer imports are fine),
and a ``features`` slice may never import a *peer* slice. This test parses every
``backend`` module with the AST and fails on any forbidden edge.

It walks *all* AST nodes, so it also catches lazy ``import`` statements buried
inside functions -- the form the historical ``database -> features`` back-edge
took before it was relocated to the ``api`` composition root.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

# What each layer MAY import (internal layers only). Same-layer imports are
# always allowed and are not listed here.
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "database": {"core"},
    "inference": {"core"},
    "analysis": {"database"},
    "workflows": {"core", "database", "inference", "analysis"},
    # ``features`` sits above ``workflows`` in the documented stack (see the
    # module docstring and AGENTS.md), so this edge is downward. It stayed off
    # the list until a slice needed it: ``features/extensions`` compiles
    # community packages into registry records and publishes them as the
    # community overlay, which is the dependency-inversion direction the design
    # calls for -- the lower layer owns the registry, the higher one fills it.
    # The reverse edge stays forbidden: ``workflows`` must not import
    # ``features``, or a built-in workflow could reach into a feature slice.
    "features": {"core", "database", "inference", "analysis", "workflows"},
    "pipeline": {"core", "database", "inference", "analysis", "workflows", "features"},
    "api": {"core", "database", "inference", "analysis", "workflows", "features", "pipeline"},
    # ``main.py`` / ``__init__.py`` sitting directly in ``backend/`` -- the
    # composition root; may wire anything below it.
    "root": {"core", "database", "inference", "analysis", "workflows", "features", "pipeline", "api"},
}
LAYERS = set(ALLOWED) - {"root"}

FEATURE_SLICES = {p.name for p in (BACKEND / "features").iterdir() if p.is_dir() and p.name != "__pycache__"}


def _iter_modules():
    """Yield (path, dotted_parts, is_init) for every backend .py module.

    Skips ``__pycache__`` and one-shot migration scripts (which use dynamic
    intra-package imports and are not living application surface)."""
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if "__pycache__" in rel.parts or "migrations" in rel.parts:
            continue
        parts = rel.with_suffix("").parts  # ("backend", "database", "bootstrap")
        is_init = path.name == "__init__.py"
        if is_init:
            parts = parts[:-1]  # the module IS the package
        yield path, parts, is_init


def _layer_of(parts: tuple[str, ...]) -> str | None:
    if len(parts) < 2 or parts[0] != "backend":
        return None
    if parts[1] in ("main", "__init__"):
        return "root"
    return parts[1]


def _resolve(parts: tuple[str, ...], is_init: bool, level: int, module: str) -> list[str]:
    """Resolve an import to absolute dotted parts, handling relative imports."""
    if level == 0:
        return module.split(".") if module else []
    pkg = list(parts) if is_init else list(parts[:-1])
    base = pkg[: len(pkg) - (level - 1)]
    return base + (module.split(".") if module else [])


def _imports(path: Path, parts: tuple[str, ...], is_init: bool):
    """Yield (target_parts, lineno, source_text) for every backend-targeting import."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _resolve(parts, is_init, node.level, node.module or "")
            if target and target[0] == "backend":
                names = ", ".join(a.name for a in node.names)
                yield target, node.lineno, f"from {'.' * node.level}{node.module or ''} import {names}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name.split(".")
                if target and target[0] == "backend":
                    yield target, node.lineno, f"import {alias.name}"


def test_no_upward_layer_imports():
    violations = []
    for path, parts, is_init in _iter_modules():
        src = _layer_of(parts)
        if src is None:
            continue
        for target, lineno, text in _imports(path, parts, is_init):
            dst = _layer_of(tuple(target))
            if dst is None or dst == src:
                continue
            if dst not in ALLOWED.get(src, set()):
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"  {src} -> {dst}   {rel}:{lineno}   ({text})")
    assert not violations, "Forbidden cross-layer imports (a layer reached up to one it may not import):\n" + "\n".join(
        sorted(violations)
    )


def test_no_peer_slice_imports():
    violations = []
    for path, parts, is_init in _iter_modules():
        if _layer_of(parts) != "features" or len(parts) < 3:
            continue
        own_slice = parts[2]
        for target, lineno, text in _imports(path, parts, is_init):
            if (
                len(target) >= 3
                and target[0] == "backend"
                and target[1] == "features"
                and target[2] in FEATURE_SLICES
                and target[2] != own_slice
            ):
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"  {own_slice} -> {target[2]}   {rel}:{lineno}   ({text})")
    assert not violations, "A features slice imported a peer slice (slices must stay isolated):\n" + "\n".join(
        sorted(violations)
    )
