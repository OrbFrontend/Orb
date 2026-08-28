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

from backend.inference.local_models import assets, dependencies
from backend.inference.prose_rewriter import runtime

ROOT = Path(__file__).resolve().parents[2]


def test_model_and_binary_dirs_resolve_under_backend_data():
    assert Path(assets.model_dir()) == ROOT / "backend" / "data" / "models"
    assert Path(runtime.bin_dir()) == ROOT / "backend" / "data" / "llama-bin"


def test_the_install_command_names_this_repos_requirements_file():
    """Same ``_ROOT``, different consumer: the pip line the Settings panel
    shows is pasted into a shell with no cwd in the repo."""
    req = dependencies.install_cmd().split("-r ", 1)[1].strip('"')
    assert Path(req) == ROOT / "requirements-ml.txt"
    assert os.path.isabs(req)
