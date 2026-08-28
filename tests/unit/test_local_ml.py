"""Pure local-ML scaffold helpers: resolve_path / present / deps_ok. No model,
no network — the route-level tri-state lives in tests/integration/test_local_ml.py.

PATCHES GO ON THE MODULE THAT OWNS THE NAME. ``local_ml`` re-exports the asset
and dependency surface for callers that address it by that name, but those are
second bindings: production reads ``local_models``' own copies, so patching a
re-export would leave the code under test running against the real disk.
"""

from __future__ import annotations

import os

from backend.inference import local_ml
from backend.inference.local_models import assets, dependencies


def test_resolve_path_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "custom.gguf"
    custom.write_bytes(b"")  # override only wins if the file exists (no stale hiding a real model)
    monkeypatch.setenv("ORB_AUTOCOMPLETE_MODEL", str(custom))
    assert assets.resolve_path("autocomplete") == str(custom)


def test_present_reflects_disk(monkeypatch):
    monkeypatch.setattr(assets, "resolve_path", lambda f: "/nope/missing.gguf")
    assert assets.present("autocomplete") is False


def test_deps_ok_reports_missing_extra():
    # ML extras aren't in the base test env; deps_ok is a cheap, honest (bool, reason).
    ok, reason = dependencies.deps_ok()
    if not ok:
        assert "requirements-ml.txt" in reason


def test_install_cmd_paths_are_absolute():
    # Pasted into a fresh cmd prompt with no cwd in the repo, so neither the
    # interpreter nor the requirements file may be relative.
    cmd = dependencies.install_cmd()
    req = cmd.split("-r ", 1)[1].strip('"')
    assert os.path.isabs(req) and req.endswith("requirements-ml.txt")
    assert os.path.isabs(cmd.split(" -m ", 1)[0].strip('"'))


def test_model_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setattr(assets, "_ROOT", str(tmp_path))
    d = assets.model_dir()
    assert os.path.isdir(d)
    assert d.endswith(os.path.join("backend", "data", "models"))


def test_pov_input_treats_every_line_break_as_a_hard_sentence_edge():
    text = "discarded first\ndiscarded second\r\nkept third\u2028kept fourth\nkept fifth"
    shaped = local_ml.pov_input(text)
    assert shaped == "kept third kept fourth kept fifth"
    assert not any(mark in shaped for mark in "\r\n\u2028")


def test_pov_input_uses_core_quote_and_sentence_policy():
    text = "Old。 ‘I don’t count.’ Second؟ 「Nor do I。」 Third. Fourth."
    assert local_ml.pov_input(text) == "Second؟ Third. Fourth."
