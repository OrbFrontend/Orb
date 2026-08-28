"""How the feature reads the shared manifest: selection, and where a file lands.

The manifest's own invariants — unique basenames, the prune claim set — are
``tests/unit/test_local_models_catalog.py``'s, because they belong to every
feature that ships weights. What is left here is the rewriter's own half: a
stored selection is user data that must survive a registry bump, and the path a
variant resolves to is the flat basename under ``data/models/`` rather than
upstream's ``GGUF/`` nesting.
"""

from __future__ import annotations

import os

from backend.features.prose_rewriter import catalog
from backend.inference.local_models import assets


def test_an_unusable_selection_resolves_to_none_rather_than_raising():
    """A fresh install has nothing selected, and a stored id is user data that a
    registry bump must not turn into a mid-turn exception."""
    assert catalog.resolve(None) is None
    assert catalog.resolve("") is None
    assert catalog.resolve("1.7b-q2-that-never-shipped") is None


def test_variant_path_is_the_flat_name_under_the_models_dir(tmp_path, monkeypatch):
    """``path`` is the layout inside the HF repo; what lands on disk is the
    basename alone."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    variant = catalog.variants()[0]
    assert "/" in variant.path  # upstream nests it under GGUF/
    assert catalog.variant_path(variant) == os.path.join(str(tmp_path), variant.local_name)
    assert catalog.on_disk(variant) is False
    (tmp_path / variant.local_name).write_text("weights")
    assert catalog.on_disk(variant) is True
