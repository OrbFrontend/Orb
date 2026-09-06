"""Focused fixtures for the shared backend dependency checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_backend_layers_fixtures", REPO_ROOT / "scripts" / "check_backend_layers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path, source: str, statement: str) -> tuple[Path, Path]:
    checker = _checker()
    root = tmp_path / "repo"
    backend = root / "backend"
    for package in checker.ALLOWED_EDGES:
        directory = backend / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (backend / "main.py").write_text("app = object()\n", encoding="utf-8")
    (backend / "workflows" / "toolkit.py").write_text("__all__ = ['forced_tool_call']\n", encoding="utf-8")
    source_path = backend / f"{source}.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(statement, encoding="utf-8")
    return root, backend


@pytest.mark.parametrize(
    ("source", "statement", "message"),
    [
        ("inference/bad", "from backend.prompting import base\n", "inference may not import prompting"),
        ("inference/bad", "from backend.main import app\n", "inference may not import root"),
        ("prompting/bad", "from backend.inference import client\n", "prompting may not import inference"),
        ("features/alpha/bad", "from backend.workflows import toolkit\n", "features may not import workflows"),
        ("workflows/bad", "from backend.features import cards\n", "workflows may not import features"),
    ],
)
def test_forbidden_edges_use_the_shared_matrix(tmp_path: Path, source: str, statement: str, message: str):
    root, backend = _fixture(tmp_path, source, statement)
    problems = _checker().check(root=root, backend=backend)
    assert any(message in problem for problem in problems), problems


def test_feature_slices_cannot_import_peers(tmp_path: Path):
    root, backend = _fixture(tmp_path, "features/alpha/bad", "from backend.features.beta import value\n")
    beta = backend / "features" / "beta"
    beta.mkdir()
    (beta / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    problems = _checker().check(root=root, backend=backend)
    assert any("feature slice 'alpha' imports peer slice 'beta'" in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("statement", "target"),
    [
        ("from backend.prompting import build_prefix\n", "backend.prompting"),
        ("from backend.inference import LLMClient\n", "backend.inference"),
        ("from backend.database import get_settings\n", "backend.database"),
        ("from backend.workflows.peer import workflow\n", "backend.workflows.peer"),
        ("from backend.workflows.registry import Workflow\n", "backend.workflows.registry"),
        ("from backend.workflows.contracts import ToolSpec\n", "backend.workflows.contracts"),
        (
            "from backend.workflows.attachment_cache import insert_workflow_attachment\n",
            "backend.workflows.attachment_cache",
        ),
    ],
)
def test_workflow_slices_import_only_their_api(tmp_path: Path, statement: str, target: str):
    root, backend = _fixture(tmp_path, "workflows/plugin/bad", statement)
    problems = _checker().check(root=root, backend=backend)
    assert any(
        f"workflow slice 'plugin' may import only its own package or workflow APIs, not {target}" in problem
        for problem in problems
    ), problems


def test_workflow_slices_may_import_own_package_and_public_apis(tmp_path: Path):
    root, backend = _fixture(
        tmp_path,
        "workflows/plugin/good",
        """from backend.workflows.toolkit import forced_tool_call
from backend.workflows.plugin.local import helper
""",
    )
    assert _checker().check(root=root, backend=backend) == []


def test_workflow_framework_modules_remain_host_adapters(tmp_path: Path):
    root, backend = _fixture(
        tmp_path,
        "workflows/toolkit",
        "__all__ = []\nfrom backend.prompting import build_prefix\n",
    )
    assert _checker().check(root=root, backend=backend) == []


@pytest.mark.parametrize(
    "statement",
    [
        "from backend.workflows.toolkit import _local_ml\n",
        "from backend.workflows.toolkit import *\n",
        "import backend.workflows.toolkit as toolkit\n",
        "from backend.workflows import toolkit\n",
        "import backend.workflows as workflows\n",
        "from backend import workflows\n",
    ],
)
def test_workflow_slices_use_only_named_public_toolkit_exports(tmp_path: Path, statement: str):
    root, backend = _fixture(tmp_path, "workflows/plugin/bad", statement)
    problems = _checker().check(root=root, backend=backend)
    assert any("workflow slice 'plugin'" in problem for problem in problems), problems


def test_python_packages_must_be_classified(tmp_path: Path):
    root, backend = _fixture(tmp_path, "inference/good", "from backend.core import value\n")
    unknown = backend / "mystery"
    unknown.mkdir()
    (unknown / "module.py").write_text("", encoding="utf-8")
    problems = _checker().check(root=root, backend=backend)
    assert any("backend/mystery/: unclassified Python package" in problem for problem in problems), problems


def test_top_level_python_modules_must_be_classified(tmp_path: Path):
    root, backend = _fixture(tmp_path, "inference/good", "from backend.core import value\n")
    (backend / "mystery.py").write_text("", encoding="utf-8")
    problems = _checker().check(root=root, backend=backend)
    assert any("backend/mystery.py: unclassified top-level Python module" in problem for problem in problems), problems
