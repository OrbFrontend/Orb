"""The variant registry, and the invariants a stale one breaks silently.

BASENAMES ARE THE WHOLE SAFETY PROPERTY. ``local_ml`` flattens every download
into ``data/models/`` because upstream repos disagree about where a GGUF lives
(root, ``gguf/``, ``GGUF/`` — two of which are ONE directory on macOS and
Windows), and ``prune_stale`` then deletes anything in that directory the specs
do not claim. So two failures are possible and neither announces itself: a
basename two specs both claim is one file two features fight over, and a
basename no spec claims is a multi-gigabyte weight ``prune_stale`` wipes the
next time an unrelated Download button is pressed.

Asserted from the registry itself rather than a hardcoded list, so a fourth
checkpoint is covered the moment it is added.
"""

from __future__ import annotations

import os

from backend.inference import local_ml
from backend.inference.prose_rewriter import catalog


def test_every_downloadable_weight_is_claimed_exactly_once():
    """The claim set is what survives a prune, and a name in it twice is one
    file two features fight over. ``filename`` names each spec's default, so it
    has to be claimed alongside the variants."""
    names = [name for spec in local_ml.MODELS.values() for name in spec.all_names()]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"these basenames are claimed twice and would collide on disk: {sorted(duplicates)}"
    for feature, spec in local_ml.MODELS.items():
        assert spec.local_name in spec.all_names(), feature
        for variant in spec.variants:
            assert variant.local_name in set(names), f"{variant.id} would be pruned as stale"


def test_the_rewriters_default_file_is_one_of_its_variants():
    """Otherwise a bare download would fetch a fourth file the selector cannot
    offer and nothing would ever load it."""
    spec = local_ml.MODELS[catalog.FEATURE]
    assert spec.local_name in {v.local_name for v in spec.variants}


def test_prune_stale_keeps_a_claimed_variant_and_removes_an_unclaimed_file(tmp_path, monkeypatch):
    """The invariant above, exercised through the function that enforces it."""
    monkeypatch.setattr(local_ml, "model_dir", lambda: str(tmp_path))
    claimed = catalog.variants()[0].local_name
    (tmp_path / claimed).write_text("weights")
    (tmp_path / "left-over-from-an-old-release.gguf").write_text("stale")

    local_ml.prune_stale(str(tmp_path))

    assert os.path.exists(tmp_path / claimed)
    assert not os.path.exists(tmp_path / "left-over-from-an-old-release.gguf")


def test_an_unusable_selection_resolves_to_none_rather_than_raising():
    """A fresh install has nothing selected, and a stored id is user data that a
    registry bump must not turn into a mid-turn exception."""
    assert catalog.resolve(None) is None
    assert catalog.resolve("") is None
    assert catalog.resolve("1.7b-q2-that-never-shipped") is None


def test_variant_path_is_the_flat_name_under_the_models_dir(tmp_path, monkeypatch):
    """``path`` is the layout inside the HF repo; what lands on disk is the
    basename alone."""
    monkeypatch.setattr(local_ml, "model_dir", lambda: str(tmp_path))
    variant = catalog.variants()[0]
    assert "/" in variant.path  # upstream nests it under GGUF/
    assert catalog.variant_path(variant) == os.path.join(str(tmp_path), variant.local_name)
    assert catalog.on_disk(variant) is False
    (tmp_path / variant.local_name).write_text("weights")
    assert catalog.on_disk(variant) is True


def test_prune_stale_keeps_every_registered_prose_variant(tmp_path, monkeypatch):
    """All three at once, not just the one the test above happened to pick.

    ``prune_stale`` reads the WHOLE registry to build its claim set, so the
    property that matters is that no variant is missing from it — a checkpoint
    the claim set forgets is 4.7 GB deleted the next time an unrelated Download
    button is pressed.
    """
    monkeypatch.setattr(local_ml, "model_dir", lambda: str(tmp_path))
    for variant in catalog.variants():
        (tmp_path / variant.local_name).write_text("weights")
    (tmp_path / "unclaimed.gguf").write_text("stale")

    local_ml.prune_stale(str(tmp_path))

    for variant in catalog.variants():
        assert os.path.exists(tmp_path / variant.local_name), variant.id
    assert not os.path.exists(tmp_path / "unclaimed.gguf")
