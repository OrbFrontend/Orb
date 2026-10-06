"""Settings are read through one checked type.

Pyright checks subscripts and value types through ``Settings`` but accepts any string in ``.get()``. These checks keep every
function and field that carries settings on the type, so the checker sees each read, and reject a literal key the type does
not declare, which is what a misspelled ``.get("...")`` would otherwise do silently.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import get_type_hints

from backend.core.settings import Settings

BACKEND = Path(__file__).resolve().parents[2] / "backend"
DECLARED = set(get_type_hints(Settings))
_SETTINGS_NAME = re.compile(r"^(settings|settings_\w+|\w+_settings)$")
_SETTINGS_TYPE = re.compile(r"^Settings( \| None)?$")


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(BACKEND.rglob("*.py")):
        if "migrations" not in path.parts:
            yield str(path.relative_to(BACKEND.parent)), ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    return (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _settings_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters typed Settings, and locals assigned from a ``get_settings()`` call."""
    args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
    names = {a.arg for a in args if a.annotation is not None and _SETTINGS_TYPE.match(ast.unparse(a.annotation))}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            value = node.value.value if isinstance(node.value, ast.Await) else node.value
            if isinstance(value, ast.Call) and ast.unparse(value.func).endswith("get_settings"):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _literal_key(node: ast.AST, is_settings) -> object:
    match node:
        case (
            ast.Subscript(value=value, slice=ast.Constant(value=key))
            | ast.Call(func=ast.Attribute(value=value, attr="get"), args=[ast.Constant(value=key), *_])
            | ast.Compare(left=ast.Constant(value=key), ops=[ast.In() | ast.NotIn(), *_], comparators=[value, *_])
        ):
            return key if is_settings(value) else None
    return None


def test_every_parameter_and_field_that_carries_settings_is_typed_settings():
    untyped = []
    for name, tree in _modules():
        for fn in _functions(tree):
            for arg in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
                annotation = ast.unparse(arg.annotation) if arg.annotation else "no annotation"
                if _SETTINGS_NAME.match(arg.arg) and not _SETTINGS_TYPE.match(annotation):
                    untyped.append(f"{name}:{fn.lineno} {fn.name}({arg.arg}: {annotation})")
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and _SETTINGS_NAME.match(node.target.id):
                if not _SETTINGS_TYPE.match(ast.unparse(node.annotation)):
                    untyped.append(f"{name}:{node.lineno} {node.target.id}: {ast.unparse(node.annotation)}")
    assert not untyped, "Type these as backend.core.settings.Settings:\n" + "\n".join(untyped)


def test_every_literal_key_read_from_settings_is_declared():
    undeclared = []
    for name, tree in _modules():
        for fn in _functions(tree):
            names = _settings_names(fn)

            def is_settings(expr: ast.AST, names: set[str] = names) -> bool:
                return (isinstance(expr, ast.Name) and expr.id in names) or (
                    isinstance(expr, ast.Attribute) and expr.attr == "settings"
                )

            for node in ast.walk(fn):
                key = _literal_key(node, is_settings)
                if isinstance(key, str) and key not in DECLARED:
                    undeclared.append(f"{name}:{node.lineno} {key!r}")
    assert not undeclared, "Settings declares no such key (a typo, or a key to add to Settings):\n" + "\n".join(undeclared)
