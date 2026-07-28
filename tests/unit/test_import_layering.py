"""Static guard for the backend's one-way layered architecture.

The dependency direction is strictly downward (see ``AGENTS.md`` and each
layer's ``__init__`` docstring):

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

# ``core`` is closed by default, not a generic home for shared helpers. Adding a
# module or a new dependency class must satisfy the Core admission rule in
# AGENTS.md and update this inventory as an explicit architecture decision.
CORE_MODULE_INVENTORY = frozenset(
    {
        "__init__",
        "domain_types",
        "llm_types",
        "locks",
        "macros",
        "personas",
        "tags",
        "utils",
        # The Writer-tool ABI. Admitted because ``workflows`` (which carries the
        # binding on a snapshot), ``features/extensions`` (which compiles the
        # callable), and ``pipeline`` (which sends the schema and invokes the
        # binding) must agree on one provider-facing name and one result
        # encoding, and none of the three may import the other two in the
        # direction that agreement needs. It holds values and pure invariants
        # only: no registry, no grants, no manifest, no I/O.
        "writer_tools",
    }
)

# A deliberately narrow import surface for the current kernel. Standard-library
# status alone is not enough: modules that enable filesystem, network,
# subprocess, environment, or persistence access do not belong in ``core``.
CORE_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "asyncio",
        "collections",
        "contextlib",
        # Value declaration only. ``dataclasses`` builds ``__init__``/``__eq__``
        # for a frozen record; it opens no file, spawns no process, and reads no
        # environment, which is the distinction this allowlist draws.
        "dataclasses",
        "datetime",
        "random",
        "re",
        "typing",
    }
)

CORE_FORBIDDEN_BUILTIN_CALLS = frozenset({"__import__", "compile", "eval", "exec", "input", "open", "print"})


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


def test_core_module_inventory_is_closed():
    core = BACKEND / "core"
    modules = {path.stem for path in core.glob("*.py")}
    subpackages = sorted(path.name for path in core.iterdir() if path.is_dir() and path.name != "__pycache__")

    assert modules == CORE_MODULE_INVENTORY, (
        "backend/core is closed by default. Before changing its module inventory, satisfy and document the Core "
        "admission rule in AGENTS.md, then update CORE_MODULE_INVENTORY explicitly.\n"
        f"  added: {sorted(modules - CORE_MODULE_INVENTORY)}\n"
        f"  removed: {sorted(CORE_MODULE_INVENTORY - modules)}"
    )
    assert not subpackages, (
        "backend/core must remain a flat, explicitly inventoried kernel; move feature-owned packages to features/: "
        f"{subpackages}"
    )


def test_core_uses_only_approved_import_surface():
    violations = []
    for path in sorted((BACKEND / "core").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots = [(node.module or "").split(".", 1)[0]]
                lineno = node.lineno
            elif isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
                lineno = node.lineno
            else:
                continue
            for root in roots:
                if root not in CORE_ALLOWED_STDLIB:
                    violations.append(f"  {path.relative_to(REPO_ROOT)}:{lineno}   ({root})")

    assert not violations, (
        "backend/core imported outside its approved dependency-free surface. Core performs no I/O and is not a "
        "generic shared-code layer; satisfy the Core admission rule before expanding CORE_ALLOWED_STDLIB:\n"
        + "\n".join(violations)
    )


def test_core_does_not_call_io_or_dynamic_code_builtins():
    violations = []
    for path in sorted((BACKEND / "core").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CORE_FORBIDDEN_BUILTIN_CALLS:
                violations.append(f"  {path.relative_to(REPO_ROOT)}:{node.lineno}   ({node.func.id})")

    assert not violations, (
        "backend/core called a builtin that performs I/O or dynamic code loading. Keep that behavior in its owning "
        "layer and pass already-loaded values into core:\n" + "\n".join(violations)
    )
