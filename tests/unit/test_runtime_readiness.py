"""Offline launch requires the mandatory runtime, without loading Orb or ML."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import check_runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("missing", [module for _, module in check_runtime.RUNTIME_IMPORTS])
def test_readiness_reports_each_required_import_and_checks_the_rest(monkeypatch, missing, capsys):
    seen = []

    def load(module):
        seen.append(module)
        if module == missing:
            raise ImportError("unavailable")
        return ModuleType(module)

    monkeypatch.setattr(check_runtime.importlib, "import_module", load)
    assert check_runtime.main() == 1
    assert seen == [module for _, module in check_runtime.RUNTIME_IMPORTS]
    assert missing in capsys.readouterr().err


def test_readiness_reports_broken_native_dependencies(monkeypatch):
    def load(module):
        if module == "PIL.Image":
            raise OSError("native library cannot be loaded")
        return ModuleType(module)

    monkeypatch.setattr(check_runtime.importlib, "import_module", load)
    assert check_runtime.runtime_dependency_errors() == ["Pillow (PIL.Image): native library cannot be loaded"]


def test_probe_has_no_application_database_or_model_side_effects(tmp_path):
    source = """
import runpy, sys

def guard(event, args):
    if event == "import":
        name = args[0].split(".")[0]
        assert name not in {"backend", "llama_cpp", "onnxruntime", "torch", "huggingface_hub"}, args[0]
sys.addaudithook(guard)
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
assert not any(name.startswith("backend") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(ROOT / "scripts/check_runtime.py")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Unix launcher; both platforms use the same Python probe")
@pytest.mark.parametrize("missing", ["", "aiosqlite"])
def test_unix_launcher_offline_requires_a_usable_runtime(tmp_path, missing):
    shutil.copy(ROOT / "run_unix.sh", tmp_path)
    shutil.copy(ROOT / "requirements.txt", tmp_path)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/check_runtime.py", tmp_path / "scripts")
    bin_dir = tmp_path / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "activate").write_text(f'export PATH="{bin_dir}:$PATH"\n')
    for command, body in {
        "pip": "exit 1",  # simulate an offline install
        "uvicorn": "touch server-started",
        "curl": "exit 0",
        "xdg-open": "exit 0",
    }.items():
        script = bin_dir / command
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    # Block just aiosqlite while fastapi and uvicorn remain importable: the old
    # readiness check incorrectly started the server in exactly this state.
    (tmp_path / "sitecustomize.py").write_text("""
import importlib, os
load = importlib.import_module

def checked(name, *args, **kwargs):
    if name == os.environ.get("ORB_TEST_MISSING"):
        raise ImportError("simulated missing package")
    return load(name, *args, **kwargs)
importlib.import_module = checked
""")
    result = subprocess.run(
        ["bash", "run_unix.sh"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "ORB_TEST_MISSING": missing},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (1 if missing else 0), result.stdout + result.stderr
    assert (tmp_path / "server-started").exists() == (not missing)
    if missing:
        assert "aiosqlite" in result.stderr
        assert not (tmp_path / "backend/data").exists()
    else:
        assert "Starting with the versions already in .venv" in result.stdout
