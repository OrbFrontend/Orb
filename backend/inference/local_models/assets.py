"""Manage local model files on disk."""

from __future__ import annotations

import os

from .catalog import MODELS, ModelVariantSpec

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
    if spec is not None and spec.variants:
        return any(variant_present(v) for v in spec.variants)
    return os.path.exists(resolve_path(feature))


def variant_path(variant: ModelVariantSpec) -> str:
    """Absolute path of *variant*'s GGUF under ``data/models/`` (may not exist)."""
    return os.path.join(model_dir(), variant.local_name)


def variant_present(variant: ModelVariantSpec) -> bool:
    return os.path.exists(variant_path(variant))


def prune_stale(root: str | None = None) -> None:
    """Delete any .gguf under data/models/ that no current MODELS spec claims.

    Runs after every download so bumping a model (e.g. v2 typeahead) doesn't leave
    the old weights eating disk. Only touches .gguf files — hf's .cache bookkeeping
    and manual drops of other extensions are left alone.

    Claim is by *basename*, not full path: comparing paths meant a model sitting in
    a legacy mirrored subdir read as unclaimed and got deleted the moment any other
    feature downloaded — and on a case-insensitive filesystem, where ``GGUF/`` and
    ``gguf/`` are one directory, that fired on a model we had just fetched.
    """
    root = root or model_dir()
    # Every basename a spec puts on disk, VARIANTS INCLUDED. A variant the claim
    # set forgets is wiped the next time any feature downloads — 4.7 GB gone
    # because an unrelated button was pressed.
    keep = {name for s in MODELS.values() for name in s.all_names()}
    walked: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".cache"]  # hf's bookkeeping is its own business
        for name in files:
            if name.endswith(".gguf") and name not in keep:
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


def download(feature: str, variant: str | None = None) -> None:
    """Fetch a GGUF into data/models/, then prune stale weights. Blocking; run in a thread."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — deferred

    repo_id, path, revision, local_name = variant_spec(feature, variant)
    got = hf_hub_download(repo_id=repo_id, filename=path, revision=revision, local_dir=model_dir())
    flat = os.path.join(model_dir(), local_name)
    if os.path.normpath(got) != os.path.normpath(flat):
        os.replace(got, flat)  # hf mirrors the repo's own gguf//GGUF/ nesting; we don't keep it
    prune_stale()  # after fetch: new file lands before old ones go, so a failed download keeps the old model


def delete_model(feature: str, variant: str | None = None) -> bool:
    """Remove one downloaded GGUF. Returns whether a file was actually deleted.

    Exists because the three rewriter variants are 9.6 GB combined and "find
    the folder yourself" is not an acceptable only exit at that size.
    """
    _repo, _path, _rev, local_name = variant_spec(feature, variant)
    target = os.path.join(model_dir(), local_name)
    if not os.path.exists(target):
        return False
    os.remove(target)
    return True


if __name__ == "__main__":
    # Self-check for the destructive prune (temp dir; never touches real models).
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        keep = os.path.join(d, MODELS["autocomplete"].local_name)
        open(keep, "w").close()
        mirrored = os.path.join(d, MODELS["pov_classifier"].filename)  # legacy gguf/ nesting
        os.makedirs(os.path.dirname(mirrored), exist_ok=True)
        open(mirrored, "w").close()
        stale = os.path.join(d, "old-granite-Q8_0.gguf")
        open(stale, "w").close()
        notes = os.path.join(d, "readme.txt")  # non-gguf must survive
        open(notes, "w").close()
        cached = os.path.join(d, ".cache", "huggingface", "download")
        os.makedirs(cached)
        prune_stale(d)
        assert os.path.exists(keep), "current spec's gguf must be kept"
        assert os.path.exists(mirrored), "a claimed gguf in a legacy subdir must survive"
        assert not os.path.exists(stale), "unclaimed gguf must be removed"
        assert os.path.exists(notes), "non-gguf must be left alone"
        assert os.path.isdir(cached), "hf's .cache must be left alone, empty or not"
    print("prune_stale OK")
