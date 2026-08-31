"""Built-in manifest of downloadable local model artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

#: How a model is executed. ``llama_cpp`` is in-process through the binding;
#: ``llama_server`` is a supervised child process spoken to over HTTP. A
#: ``Literal`` rather than a bare ``str`` so a typo fails at the definition.
RuntimeKind = Literal["llama_cpp", "llama_server"]


@dataclass(frozen=True)
class ModelVariantSpec:
    """One downloadable checkpoint of a feature that ships several.

    ``label``/``detail`` are presentation: the Local ML panel renders them for
    ANY variant-bearing feature, which is why they live on the artifact record
    rather than in the feature that happens to have variants today.

    ``path`` is the path *inside the HF repo* (upstream's ``GGUF/`` layout);
    ``local_name`` is the flat basename ``assets.download`` writes under
    ``data/models/``, and the name ``prune_stale`` must claim.
    """

    id: str
    label: str
    detail: str
    repo_id: str
    path: str
    revision: str  # pinned commit sha — a repo re-point can't swap the weights under us
    size_mb: int

    @property
    def local_name(self) -> str:
        return os.path.basename(self.path)


@dataclass(frozen=True)
class ModelSpec:
    """Describe one local-ML feature and its artifacts."""

    repo_id: str
    filename: str  # path *inside the HF repo* — upstream's layout, not ours
    size_mb: int
    revision: str  # pinned commit sha — a repo re-point can't swap the weights under us
    runtime: RuntimeKind = "llama_cpp"
    variants: tuple[ModelVariantSpec, ...] = ()

    @property
    def local_name(self) -> str:
        """On-disk name under data/models/, always flat.

        Upstream repos disagree about where a GGUF lives — root, ``gguf/``,
        ``GGUF/`` — and mirroring that gave us a tree whose two case-variant
        directories are ONE directory on macOS/Windows. Basenames must stay
        unique across MODELS *and* across every spec's variants — a name two
        specs both claim is one file two features would fight over, and a
        variant name no spec claims is a file ``prune_stale`` deletes the next
        time anything downloads. ``test_local_models_catalog`` asserts both.
        """
        return os.path.basename(self.filename)

    def all_names(self) -> set[str]:
        """Every basename this spec puts under data/models/ — the prune claim."""
        return {self.local_name, *(v.local_name for v in self.variants)}


# The two prose-rewriter repos, pinned. Named once because three variants
# share them and a half-updated pin is a silently different model.
_PROSE_1_7B_REPO = "chartreuse-verte/prose-rewriter-1.7b-v1.2"
_PROSE_1_7B_REV = "9376423c82bf236c9cfc8d79fa1675fa104d4979"
_PROSE_4B_REPO = "chartreuse-verte/prose-rewriter-4b-v1.2"
_PROSE_4B_REV = "b73aa3290a81024389e06c9d525238071ccbe480"

MODELS: dict[str, ModelSpec] = {
    "autocomplete": ModelSpec(
        repo_id="chartreuse-verte/orb-human-typeahead-1b-v2.2",
        filename="GGUF/orb-human-typeahead-1b-v2.2-Q4_0.gguf",
        size_mb=930,
        revision="2e340db799eca2ef36ef80fc6938e40ab1ece111",
    ),
    "slop_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin150m-purple-GGUF",
        filename="ettin150m-purple-q8_0.gguf",
        size_mb=161,
        revision="125cf38d62e78b7091c23e6d523d805c7ec2f47e",
    ),
    "emotion_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin-emotion-28-multilabel-68m",
        filename="gguf/ettin-emotion-28ml-68m-q8_0.gguf",
        size_mb=71,
        revision="9f8d0100e45c133e713283499e55105f61d29118",
    ),
    "pov_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin-povtense-17m",
        filename="gguf/povtense-17m-q8_0.gguf",
        size_mb=20,
        revision="1245e55c47f9afc3d4938ef70f5228580228d899",
    ),
    # Not an in-process model: served by a child llama-server (see
    # local_models/llama_server/, driven by features/prose_rewriter/).
    # `filename`/`size_mb` name the default variant so the legacy single-file
    # paths keep working; the selector reads `variants`, and every basename
    # here must also be claimed by prune_stale.
    "prose_rewriter": ModelSpec(
        repo_id=_PROSE_4B_REPO,
        filename="GGUF/prose-rewriter-4b-v1.2-Q8_0.gguf",
        size_mb=4694,
        revision=_PROSE_4B_REV,
        runtime="llama_server",
        variants=(
            ModelVariantSpec(
                id="1.7b-q8",
                label="1.7B · Q8_0",
                detail="Fastest, good enough.",
                repo_id=_PROSE_1_7B_REPO,
                path="GGUF/prose-rewriter-1.7b-v1.2-Q8_0.gguf",
                revision=_PROSE_1_7B_REV,
                size_mb=2165,
            ),
            ModelVariantSpec(
                id="4b-q4km",
                label="4B · Q4_K_M",
                detail="Medium quality.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v1.2-Q4_K_M.gguf",
                revision=_PROSE_4B_REV,
                size_mb=2716,
            ),
            ModelVariantSpec(
                id="4b-q8",
                label="4B · Q8_0",
                detail="Best quality, invents the least.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v1.2-Q8_0.gguf",
                revision=_PROSE_4B_REV,
                size_mb=4694,
            ),
        ),
    ),
}
