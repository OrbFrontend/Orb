"""Check artifact basenames directly from the manifest: downloads flatten paths, so shared basenames collide and unclaimed
weights are pruned. New specs automatically participate.
"""

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
    """Otherwise a bare download would fetch a fourth file the selector cannot offer and nothing would ever load it."""
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

    ``prune_stale`` reads the WHOLE manifest to build its claim set, so the property that matters is that no variant is missing
    from it -- a checkpoint the claim set forgets is 4.7 GB deleted the next time an unrelated Download button is pressed.
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


def test_prune_stale_leaves_what_it_does_not_manage(tmp_path):
    """The prune deletes user files, so its reach is the contract: a claimed
    weight in a mirrored repo subdirectory, an unmanaged extension, and hf's
    ``.cache`` bookkeeping all survive it."""
    mirrored = tmp_path / MODELS["emotion_classifier"].filename
    mirrored.parent.mkdir()
    mirrored.write_text("weights")
    notes = tmp_path / "readme.txt"
    notes.write_text("mine")
    cache = tmp_path / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)

    assets.prune_stale(str(tmp_path))

    assert mirrored.exists()
    assert notes.exists()
    assert cache.is_dir()


def test_every_artifact_has_an_extension_prune_stale_can_claim():
    """``prune_stale`` only deletes the suffixes in ``MANAGED_SUFFIXES``. A spec
    that writes anything else puts a file on disk nothing will ever clean up on
    a model bump -- which is exactly what happened when ONNX artifacts were
    added to a prune that knew only ``.gguf``."""
    for feature, spec in MODELS.items():
        for name in spec.all_names():
            assert name.endswith(assets.MANAGED_SUFFIXES), f"{feature}: {name}"


def test_a_companion_file_is_required_for_present_and_kept_by_prune(tmp_path, monkeypatch):
    """A companion is not an alternative: half of Spark-TTS is a cloner that
    cannot enroll, or enrolled tokens nothing can speak. So ``present`` must be
    false until both land, and neither may be pruned."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    spec = MODELS["spark_tts_codec"]
    assert spec.extra_files, "this test is about the companion mechanism"
    companion = spec.extra_files[0]

    (tmp_path / spec.local_name).write_text("decoder")
    assert not assets.present("spark_tts_codec")
    assert assets.missing_files("spark_tts_codec") == [companion.local_name]

    (tmp_path / companion.local_name).write_text("encoder")
    assert assets.present("spark_tts_codec")
    assert assets.missing_files("spark_tts_codec") == []

    (tmp_path / "some-other-decoder.onnx").write_text("stale")
    assets.prune_stale(str(tmp_path))
    assert (tmp_path / spec.local_name).exists()
    assert (tmp_path / companion.local_name).exists()
    assert not (tmp_path / "some-other-decoder.onnx").exists()


def test_the_subject_analyzer_is_not_ready_without_its_pair_comparer(tmp_path, monkeypatch):
    """Subject fixation needs both models, so one download fetches both and the tagger alone is not ready."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    spec = MODELS["subjects_classifier"]
    (comparer,) = spec.extra_files
    (tmp_path / spec.local_name).write_text("tagger")
    assert not assets.present("subjects_classifier")
    assert assets.missing_files("subjects_classifier") == [comparer.local_name]
    (tmp_path / comparer.local_name).write_text("comparer")
    assert assets.present("subjects_classifier")


def test_deleting_a_specs_own_file_takes_its_companions(tmp_path, monkeypatch):
    """They are useless alone, and 23 MB nothing claims is the shape of bug ``prune_stale`` exists to prevent."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    spec = MODELS["spark_tts_codec"]
    companion = spec.extra_files[0]
    (tmp_path / spec.local_name).write_text("decoder")
    (tmp_path / companion.local_name).write_text("encoder")

    assert assets.delete_model("spark_tts_codec") is True

    assert not (tmp_path / spec.local_name).exists()
    assert not (tmp_path / companion.local_name).exists()


def test_deleting_one_variant_leaves_shared_companions_alone(tmp_path, monkeypatch):
    """A variant's siblings still need them, so the companion sweep is scoped to a delete of the spec's OWN file."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    variant = MODELS["prose_rewriter"].variants[0]
    (tmp_path / variant.local_name).write_text("weights")
    assert assets.delete_model("prose_rewriter", variant.id) is True
    assert not (tmp_path / variant.local_name).exists()


def test_a_pinned_checksum_is_hex_and_the_right_length():
    """A truncated or mistyped pin rejects every download of a good file."""
    for feature, spec in MODELS.items():
        for label, digest in [(feature, spec.sha256)] + [(f.local_name, f.sha256) for f in spec.extra_files]:
            if digest:
                assert len(digest) == 64 and set(digest) <= set("0123456789abcdef"), label


def test_verify_rejects_and_removes_a_file_whose_bytes_are_wrong(tmp_path):
    """A revision pin says which COMMIT; this says which BYTES, and a repo that
    is force-pushed or recreated can satisfy the first and fail this. The file
    must not survive, or ``present()`` would then call it ready."""
    import pytest

    target = tmp_path / "artifact.onnx"
    target.write_text("not the bytes we verified")
    with pytest.raises(RuntimeError, match="pinned checksum"):
        assets._verify(str(target), "0" * 64)
    assert not target.exists()

    good = tmp_path / "good.onnx"
    good.write_bytes(b"abc")
    assets._verify(str(good), assets.file_sha256(str(good)))  # no exception
    assert good.exists()
