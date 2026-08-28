"""The backend layer stack, enforced rather than described.

``scripts/check_backend_layers.py`` is wired into ``scripts/lint.sh``; this
runs it from the suite too, because a layering violation is the kind of thing
that gets written and committed between two lint runs. The check parses
imports — see the script's docstring for why a grep is the wrong tool here.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _checker():
    spec = importlib.util.spec_from_file_location("check_backend_layers", ROOT / "scripts" / "check_backend_layers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_backend_module_imports_upward_or_sideways():
    assert _checker().check() == []


def test_the_shared_local_model_layer_stays_below_its_callers():
    """The edge this refactor exists to delete, asserted by name.

    ``inference/local_models/`` is shared infrastructure: the prose rewriter is
    a *caller* of it. An import back up into ``features/`` (or ``pipeline/``,
    or ``api/``) would restore the cycle the split removed, and it would still
    load fine — the deferred-import trick that hid the last one is always
    available.
    """
    checker = _checker()
    shared = ROOT / "backend" / "inference" / "local_models"
    if not shared.exists():  # the package lands in Phase 1 of the split
        return
    for path in sorted(shared.rglob("*.py")):
        package = checker._package_parts(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for target in checker._targets(node, package):
                where = f"{path.relative_to(ROOT)}:{node.lineno}"
                assert target[:2] not in (
                    ["backend", "features"],
                    ["backend", "pipeline"],
                    ["backend", "api"],
                    ["backend", "workflows"],
                ), where
