"""Where the weights and the llama-server binary actually live.

THE FAILURE THIS EXISTS FOR DOES NOT RAISE. Both directories are derived by
counting ``__file__`` up to the repo root, and a module that moves one level
deeper without its count moving with it resolves to
``backend/inference/data/models/`` instead: ``model_dir()`` creates it happily,
every model then reports as missing, and the next Download button pulls 9.6 GB
into the wrong place. Nothing anywhere says so.

Pinned against the repo root computed from *this* file, which sits at a known
depth of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.inference.local_models import assets, dependencies
from backend.inference.local_models.llama_server import binary

ROOT = Path(__file__).resolve().parents[2]

# Every test here asserts on the resolved directory itself, either against the
# real repo root or against a patched ``_ROOT``. The suite-wide fixture that
# stubs ``model_dir`` out to an empty directory would answer for all of them.
pytestmark = pytest.mark.real_model_dir


def test_model_and_binary_dirs_resolve_under_backend_data():
    assert Path(assets.model_dir()) == ROOT / "backend" / "data" / "models"
    assert Path(binary.bin_dir()) == ROOT / "backend" / "data" / "llama-bin"


def test_the_install_command_names_this_repos_requirements_file():
    """Same ``_ROOT``, different consumer: the pip line the Settings panel
    shows is pasted into a shell with no cwd in the repo."""
    req = dependencies.install_cmd().split("-r ", 1)[1].strip('"')
    assert Path(req) == ROOT / "requirements-ml.txt"
    assert os.path.isabs(req)


def test_pov_v1_weights_are_not_mistaken_for_an_installed_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "_ROOT", str(tmp_path))
    model_dir = Path(assets.model_dir())
    legacy = model_dir / "povtense-17m-q8_0.gguf"
    legacy.write_bytes(b"v1")
    mirrored = model_dir / "gguf" / legacy.name
    mirrored.parent.mkdir()
    mirrored.write_bytes(b"v1")
    (tmp_path / legacy.name).write_bytes(b"v1")
    assert assets.present("pov_classifier") is False
    current = Path(assets.resolve_path("pov_classifier"))
    assert current == model_dir / "povtense-17m-v2-q8_0.gguf"
    current.write_bytes(b"v2")
    assert assets.present("pov_classifier") is True
    assets.prune_stale()
    assert current.read_bytes() == b"v2"
    assert not legacy.exists() and not mirrored.exists()


def test_pov_download_uses_the_upstream_name_but_stores_the_versioned_alias(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(assets, "_ROOT", str(tmp_path))

    def fetch(*, repo_id, filename, revision, local_dir):
        assert repo_id.endswith("/ettin-povtense-17m-v2")
        assert filename == "gguf/povtense-17m-q8_0.gguf"
        assert revision == assets.MODELS["pov_classifier"].revision
        target = Path(local_dir) / filename
        target.parent.mkdir()
        target.write_bytes(b"downloaded v2")
        return str(target)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=fetch))
    assets.download("pov_classifier")
    assert Path(assets.resolve_path("pov_classifier")).read_bytes() == b"downloaded v2"
