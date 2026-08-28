"""The shared artifact manifest, and the invariants a stale one breaks silently.

BASENAMES ARE THE WHOLE SAFETY PROPERTY. ``assets`` flattens every download
into ``data/models/`` because upstream repos disagree about where a GGUF lives
(root, ``gguf/``, ``GGUF/`` — two of which are ONE directory on macOS and
Windows), and ``prune_stale`` then deletes anything in that directory the specs
do not claim. So two failures are possible and neither announces itself: a
basename two specs both claim is one file two features fight over, and a
basename no spec claims is a multi-gigabyte weight ``prune_stale`` wipes the
next time an unrelated Download button is pressed.

Asserted from the manifest itself rather than a hardcoded list, so a fourth
checkpoint is covered the moment it is added.
"""

from __future__ import annotations

import os

from backend.inference.local_models import assets
from backend.inference.local_models.catalog import MODELS


def test_every_downloadable_basename_is_claimed_exactly_once():
    """The claim set is what survives a prune, and a name in it twice is one
    file two features fight over. ``filename`` names each spec's default, so it
    has to be claimed alongside the variants."""
    names = [name for spec in MODELS.values() for name in spec.all_names()]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"these basenames are claimed twice and would collide on disk: {sorted(duplicates)}"
    for feature, spec in MODELS.items():
        assert spec.local_name in spec.all_names(), feature
        for variant in spec.variants:
            assert variant.local_name in set(names), f"{variant.id} would be pruned as stale"


def test_a_variant_bearing_specs_default_file_is_one_of_its_variants():
    """Otherwise a bare download would fetch a fourth file the selector cannot
    offer and nothing would ever load it."""
    for feature, spec in MODELS.items():
        if spec.variants:
            assert spec.local_name in {v.local_name for v in spec.variants}, feature


def test_prune_stale_keeps_a_claimed_variant_and_removes_an_unclaimed_file(tmp_path, monkeypatch):
    """The invariant above, exercised through the function that enforces it."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    claimed = MODELS["prose_rewriter"].variants[0].local_name
    (tmp_path / claimed).write_text("weights")
    (tmp_path / "left-over-from-an-old-release.gguf").write_text("stale")

    assets.prune_stale(str(tmp_path))

    assert os.path.exists(tmp_path / claimed)
    assert not os.path.exists(tmp_path / "left-over-from-an-old-release.gguf")


def test_prune_stale_keeps_every_registered_prose_variant(tmp_path, monkeypatch):
    """All three at once, not just the one the test above happened to pick.

    ``prune_stale`` reads the WHOLE manifest to build its claim set, so the
    property that matters is that no variant is missing from it — a checkpoint
    the claim set forgets is 4.7 GB deleted the next time an unrelated Download
    button is pressed.
    """
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    variants = MODELS["prose_rewriter"].variants
    for variant in variants:
        (tmp_path / variant.local_name).write_text("weights")
    (tmp_path / "unclaimed.gguf").write_text("stale")

    assets.prune_stale(str(tmp_path))

    for variant in variants:
        assert os.path.exists(tmp_path / variant.local_name), variant.id
    assert not os.path.exists(tmp_path / "unclaimed.gguf")
