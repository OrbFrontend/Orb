"""Pure local-ML scaffold helpers: resolve_path / present / deps_ok. No model,
no network — the route-level tri-state lives in tests/integration/test_local_ml.py."""

from __future__ import annotations

import os

from backend.inference import local_ml


def test_resolve_path_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "custom.gguf"
    custom.write_bytes(b"")  # override only wins if the file exists (no stale hiding a real model)
    monkeypatch.setenv("ORB_AUTOCOMPLETE_MODEL", str(custom))
    assert local_ml.resolve_path("autocomplete") == str(custom)


def test_present_reflects_disk(monkeypatch):
    monkeypatch.setattr(local_ml, "resolve_path", lambda f: "/nope/missing.gguf")
    assert local_ml.present("autocomplete") is False


def test_deps_ok_reports_missing_extra():
    # ML extras aren't in the base test env; deps_ok is a cheap, honest (bool, reason).
    ok, reason = local_ml.deps_ok()
    if not ok:
        assert "requirements-ml.txt" in reason


def test_model_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setattr(local_ml, "_ROOT", str(tmp_path))
    d = local_ml.model_dir()
    assert os.path.isdir(d)
    assert d.endswith(os.path.join("backend", "data", "models"))
