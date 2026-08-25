"""The variant registry, and the two invariants a stale one breaks silently.

BASENAMES ARE THE WHOLE SAFETY PROPERTY. ``local_ml`` flattens every download
into ``data/models/`` because upstream repos disagree about where a GGUF lives
(root, ``gguf/``, ``GGUF/`` — two of which are ONE directory on macOS and
Windows), and ``prune_stale`` then deletes anything in that directory the specs
do not claim. So two failures are possible and neither announces itself: a
basename two specs both claim is one file two features fight over, and a
basename no spec claims is a multi-gigabyte weight ``prune_stale`` wipes the
next time an unrelated Download button is pressed.

Both are asserted from the registry itself rather than a hardcoded list, so a
fourth checkpoint is covered the moment it is added.
"""

from __future__ import annotations

import os

import pytest

from backend.inference import local_ml
from backend.inference.prose_rewriter import catalog


def _every_claimed_name() -> list[str]:
    return [name for spec in local_ml.MODELS.values() for name in spec.all_names()]


def test_no_two_registered_weights_share_a_basename():
    names = _every_claimed_name()
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"these basenames are claimed twice and would collide on disk: {sorted(duplicates)}"


def test_prune_stale_claims_every_variant_it_can_download():
    """The claim set is what survives a prune. A variant missing from it is
    4.7 GB deleted because someone downloaded something else."""
    keep = {name for s in local_ml.MODELS.values() for name in s.all_names()}
    for spec in local_ml.MODELS.values():
        for variant in spec.variants:
            assert variant.local_name in keep, f"{variant.id} would be pruned as stale"


def test_a_specs_own_filename_is_claimed_alongside_its_variants():
    """``filename`` names the default so the pre-variant paths (``resolve_path``,
    a bare ``download``) keep working; it has to be in the claim set too."""
    for feature, spec in local_ml.MODELS.items():
        assert spec.local_name in spec.all_names(), feature


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


# ── the catalog's own view ───────────────────────────────────────────────────


def test_both_halves_read_the_same_variant_rows():
    """``local_ml.MODELS`` owns the download plumbing and ``catalog`` owns the
    selector; a fourth checkpoint must reach both from one entry."""
    assert catalog.variants() is local_ml.MODELS[catalog.FEATURE].variants


def test_the_default_id_names_a_registered_variant():
    assert catalog.DEFAULT_ID in {v.id for v in catalog.variants()}


def test_nothing_selected_resolves_to_none_rather_than_raising():
    """A fresh install has an empty models dir and the feature simply does not
    run — ``None`` is a supported state, not an error."""
    assert catalog.resolve(None) is None
    assert catalog.resolve("") is None


def test_a_retired_id_resolves_to_none_rather_than_breaking_a_turn():
    """The stored selection is user data; a registry bump must not raise
    mid-turn."""
    assert catalog.resolve("1.7b-q2-that-never-shipped") is None


def test_get_is_the_strict_lookup_and_names_the_alternatives():
    with pytest.raises(ValueError, match="Unknown prose-rewriter model"):
        catalog.get("1.7b-q2-that-never-shipped")
    assert catalog.get(catalog.DEFAULT_ID).id == catalog.DEFAULT_ID


def test_variant_path_is_the_flat_name_under_the_models_dir(tmp_path, monkeypatch):
    """``path`` is the layout inside the HF repo; what lands on disk is the
    basename alone."""
    monkeypatch.setattr(local_ml, "model_dir", lambda: str(tmp_path))
    variant = catalog.get(catalog.DEFAULT_ID)
    assert "/" in variant.path  # upstream nests it under GGUF/
    assert catalog.variant_path(variant) == os.path.join(str(tmp_path), variant.local_name)
    assert catalog.on_disk(variant) is False
    (tmp_path / variant.local_name).write_text("weights")
    assert catalog.on_disk(variant) is True
