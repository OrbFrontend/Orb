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
