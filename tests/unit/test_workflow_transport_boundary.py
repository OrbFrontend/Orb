"""The workflows layer must not depend on HTTP transport.

Workflow hooks return transport-neutral domain results (a dict, or a
``WorkflowEventStream``); turning those into HTTP/SSE responses is the API
layer's job. A Starlette/FastAPI import anywhere under ``backend/workflows``
would re-introduce exactly the coupling the on-demand streaming refactor
removed, so this guards the boundary permanently.
"""

from __future__ import annotations

import ast
import pathlib

import backend.workflows

_FORBIDDEN_TOP_LEVEL = {"starlette", "fastapi"}


def _imported_top_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def test_no_workflow_module_imports_http_transport():
    root = pathlib.Path(backend.workflows.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        forbidden = _imported_top_modules(path) & _FORBIDDEN_TOP_LEVEL
        if forbidden:
            offenders.append(f"{path.relative_to(root)}: {sorted(forbidden)}")
    assert not offenders, "workflows must stay HTTP-transport-free: " + "; ".join(offenders)
