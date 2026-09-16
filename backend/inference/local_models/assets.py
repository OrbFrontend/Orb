"""Manage local model files on disk."""

from __future__ import annotations

import hashlib
import os

from .catalog import MODELS, ModelFileSpec, ModelVariantSpec

#: Extensions ``prune_stale`` is allowed to delete. Every artifact the catalog
#: puts on disk must end in one of these, or the file becomes unclaimable
#: garbage that nothing ever cleans up on a model bump. Asserted by
#: ``test_local_models_catalog``.
MANAGED_SUFFIXES = (".gguf", ".onnx")

#: The repo root: four directories up from ``backend/inference/local_models/``.
#: A wrong count here does not raise — it silently creates a second, empty
#: models directory and reports every downloaded weight as missing. Pinned by
#: ``tests/unit/test_local_models_paths.py``.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def model_dir() -> str:
    d = os.path.join(_ROOT, "backend", "data", "models")
    os.makedirs(d, exist_ok=True)
    return d


def resolve_path(feature: str) -> str:
    """Where feature's GGUF lives: env override → data/models → repo root (back-compat)."""
    if feature == "autocomplete":
        env = os.environ.get("ORB_AUTOCOMPLETE_MODEL")
        if env and os.path.exists(env):  # stale override must not hide a downloaded model
            return env
    spec = MODELS[feature]
    if spec.local_filename:
        # An explicit alias separates versions whose upstream basenames collide.
        # The old mirrored/root paths cannot establish which weights they hold.
        return os.path.join(model_dir(), spec.local_name)
    for candidate in (
        os.path.join(model_dir(), spec.local_name),  # flat — what download() writes
        os.path.join(model_dir(), spec.filename),  # legacy: hf's mirror of the repo layout
        os.path.join(_ROOT, spec.local_name),  # legacy: manual drop at repo root
        os.path.join(_ROOT, spec.filename),  # legacy: mirrored drop at repo root
    ):
        if os.path.exists(candidate):
            return candidate
    return os.path.join(model_dir(), spec.local_name)  # absent: name the flat path in errors


def present(feature: str) -> bool:
    """Is *feature* usable from disk?

    For a variant-bearing spec that means ANY variant is downloaded — the
    Settings card flips from "download something" to "pick one and enable" on
    the first file, not on the default one.
    """
    spec = MODELS.get(feature)
    if spec is None:
        return os.path.exists(resolve_path(feature))
    # A companion is not optional: half of Spark-TTS is a cloner that cannot
    # enroll, or enrolled tokens nothing can speak. Checked before the variant
    # branch so it applies to both shapes.
    if any(not os.path.exists(file_path(f)) for f in spec.extra_files):
        return False
    if spec.variants:
        return any(variant_present(v) for v in spec.variants)
    return os.path.exists(resolve_path(feature))


def variant_path(variant: ModelVariantSpec) -> str:
    """Absolute path of *variant*'s GGUF under ``data/models/`` (may not exist)."""
    return os.path.join(model_dir(), variant.local_name)


def variant_present(variant: ModelVariantSpec) -> bool:
    return os.path.exists(variant_path(variant))


def file_path(spec: ModelFileSpec) -> str:
    """Absolute path of a companion file under ``data/models/`` (may not exist)."""
    return os.path.join(model_dir(), spec.local_name)


def missing_files(feature: str) -> list[str]:
    """Basenames of *feature*'s artifacts that are not on disk.

    For error messages that say WHICH file is missing. "Spark-TTS is not
    downloaded" when only the 23 MB encoder is absent sends the user to
    re-fetch 368 MB they already have.
    """
    spec = MODELS.get(feature)
    if spec is None:
        return []
    absent = [f.local_name for f in spec.extra_files if not os.path.exists(file_path(f))]
    if spec.variants:
        if not any(variant_present(v) for v in spec.variants):
            absent.append(spec.local_name)
    elif not os.path.exists(resolve_path(feature)):
        absent.append(spec.local_name)
    return absent


def prune_stale(root: str | None = None) -> None:
    """Delete any .gguf under data/models/ that no current MODELS spec claims.

    Runs after every download so bumping a model (e.g. v2 typeahead) doesn't leave
    the old weights eating disk. Only touches the extensions the catalog itself
    writes (``MANAGED_SUFFIXES``) — hf's .cache bookkeeping and manual drops of
    anything else are left alone.

    Claim is by *basename*, not full path: comparing paths meant a model sitting in
    a legacy mirrored subdir read as unclaimed and got deleted the moment any other
    feature downloaded — and on a case-insensitive filesystem, where ``GGUF/`` and
    ``gguf/`` are one directory, that fired on a model we had just fetched.
    """
    root = root or model_dir()
    # Every basename a spec puts on disk, VARIANTS AND COMPANIONS INCLUDED. A
    # name the claim set forgets is wiped the next time any feature downloads —
    # 4.7 GB gone because an unrelated button was pressed.
    keep = {name for s in MODELS.values() for name in s.all_names()}
    walked: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".cache"]  # hf's bookkeeping is its own business
        for name in files:
            if name.endswith(MANAGED_SUFFIXES) and name not in keep:
                os.remove(os.path.join(dirpath, name))
        if dirpath != root:
            walked.append(dirpath)
    for d in reversed(walked):  # deepest first, so the emptied-out gguf//GGUF/ mirrors collapse
        if not os.listdir(d):
            os.rmdir(d)


def variant_spec(feature: str, variant_id: str | None) -> tuple[str, str, str, str]:
    """``(repo_id, path_in_repo, revision, local_name)`` for one download.

    A ``variant_id`` names one of the spec's variants; ``None`` falls back to
    the spec's own file, which is what every single-file feature uses.
    """
    spec = MODELS[feature]
    if variant_id:
        for v in spec.variants:
            if v.id == variant_id:
                return v.repo_id, v.path, v.revision, v.local_name
        raise ValueError(f"Unknown variant {variant_id!r} for {feature!r}")
    return spec.repo_id, spec.filename, spec.revision, spec.local_name


def file_sha256(path: str) -> str:
    """Hex sha256 of a file, read in chunks so a 4.7 GB weight is not a 4.7 GB read."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify(path: str, expected: str) -> None:
    """Reject a file whose bytes are not the ones that were verified.

    A revision pin says WHICH commit; this says which BYTES, and the two are
    not the same promise — a repo that is force-pushed, deleted and recreated,
    or replaced by a namespace takeover can satisfy the first and fail this. A
    mismatch deletes the download rather than leaving a file that `present()`
    would then call ready.
    """
    if not expected:
        return
    actual = file_sha256(path)
    if actual == expected:
        return
    os.remove(path)
    raise RuntimeError(
        f"{os.path.basename(path)} does not match its pinned checksum "
        f"(expected {expected[:16]}…, got {actual[:16]}…); the download was discarded."
    )


def _fetch(repo_id: str, path: str, revision: str, local_name: str, sha256: str) -> None:
    """One file into ``data/models/<local_name>``, flattened and checked."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — deferred

    got = hf_hub_download(repo_id=repo_id, filename=path, revision=revision, local_dir=model_dir())
    flat = os.path.join(model_dir(), local_name)
    if os.path.normpath(got) != os.path.normpath(flat):
        os.replace(got, flat)  # hf mirrors the repo's own gguf//GGUF/ nesting; we don't keep it
    _verify(flat, sha256)


def download(feature: str, variant: str | None = None) -> None:
    """Fetch a feature's artifacts into data/models/, then prune stale weights.

    Blocking; run in a thread. Companions come down in the same press as the
    main file — a half-fetched Spark-TTS is a feature that reports present and
    then fails at the first enrollment.
    """
    spec = MODELS[feature]
    repo_id, path, revision, local_name = variant_spec(feature, variant)
    # Companions first, because they are the small ones: a failure there costs
    # seconds rather than stranding a user who just waited for 368 MB.
    for companion in spec.extra_files:
        if not os.path.exists(file_path(companion)):
            _fetch(companion.repo_id, companion.path, companion.revision, companion.local_name, companion.sha256)
    # A variant download carries no per-variant checksum; the spec's own hash
    # describes the spec's own file and must not be applied to a sibling.
    _fetch(repo_id, path, revision, local_name, spec.sha256 if variant is None else "")
    prune_stale()  # after fetch: new file lands before old ones go, so a failed download keeps the old model


def delete_model(feature: str, variant: str | None = None) -> bool:
    """Remove one downloaded artifact. Returns whether anything was deleted.

    Exists because the three rewriter variants are 9.6 GB combined and "find
    the folder yourself" is not an acceptable only exit at that size.

    Deleting a spec's own file takes its companions with it: they are useless
    alone, and leaving 23 MB behind that nothing claims is the shape of bug
    ``prune_stale`` exists to prevent. Deleting one VARIANT leaves them, since
    its siblings still need them.
    """
    spec = MODELS[feature]
    _repo, _path, _rev, local_name = variant_spec(feature, variant)
    targets = [os.path.join(model_dir(), local_name)]
    if variant is None:
        targets += [file_path(f) for f in spec.extra_files]
    removed = False
    for target in targets:
        if os.path.exists(target):
            os.remove(target)
            removed = True
    return removed


if __name__ == "__main__":
    # Self-check for the destructive prune (temp dir; never touches real models).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        keep = os.path.join(d, MODELS["autocomplete"].local_name)
        open(keep, "w").close()
        mirrored = os.path.join(d, MODELS["emotion_classifier"].filename)  # legacy gguf/ nesting
        os.makedirs(os.path.dirname(mirrored), exist_ok=True)
        open(mirrored, "w").close()
        companion = os.path.join(d, next(iter(MODELS["spark_tts_codec"].extra_files)).local_name)
        open(companion, "w").close()
        stale = os.path.join(d, "old-granite-Q8_0.gguf")
        open(stale, "w").close()
        stale_onnx = os.path.join(d, "left-over-decoder.onnx")
        open(stale_onnx, "w").close()
        notes = os.path.join(d, "readme.txt")  # an unmanaged extension must survive
        open(notes, "w").close()
        cached = os.path.join(d, ".cache", "huggingface", "download")
        os.makedirs(cached)
        prune_stale(d)
        assert os.path.exists(keep), "current spec's gguf must be kept"
        assert os.path.exists(mirrored), "a claimed gguf in a legacy subdir must survive"
        assert os.path.exists(companion), "a claimed companion .onnx must be kept"
        assert not os.path.exists(stale), "unclaimed gguf must be removed"
        assert not os.path.exists(stale_onnx), "unclaimed onnx must be removed"
        assert os.path.exists(notes), "an unmanaged extension must be left alone"
        assert os.path.isdir(cached), "hf's .cache must be left alone, empty or not"
    print("prune_stale OK")
