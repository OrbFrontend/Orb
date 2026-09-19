"""Built-in manifest of downloadable local model artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

#: How a model is executed. ``llama_cpp`` is in-process through the binding;
#: ``llama_server`` is a supervised child process spoken to over HTTP; ``onnx``
#: is a cached ONNX Runtime session (see ``local_models/onnx_runtime/``). A
#: ``Literal`` rather than a bare ``str`` so a typo fails at the definition.
RuntimeKind = Literal["llama_cpp", "llama_server", "onnx"]


@dataclass(frozen=True)
class ModelFileSpec:
    """A companion file that must travel WITH a spec's main artifact.

    Not a variant. A variant is an alternative the user picks between and any
    one of which makes the feature work; a companion is a second file the
    feature does not run without — Spark-TTS needs both the bicodec decoder and
    the speaker encoder, and half of that pair is a voice cloner that cannot
    enroll or a set of enrolled tokens that cannot be spoken.

    So ``present()`` requires every companion, ``download()`` fetches them in
    one press, and ``prune_stale`` claims their basenames.
    """

    repo_id: str
    path: str  # path *inside* the HF repo
    revision: str  # pinned commit sha
    size_mb: int
    sha256: str = ""  # verified after download when set; see assets.download

    @property
    def local_name(self) -> str:
        return os.path.basename(self.path)


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
    local_filename: str = ""  # versioned alias when upstream reuses an older artifact's basename
    extra_files: tuple[ModelFileSpec, ...] = ()  # companions fetched with the main file
    sha256: str = ""  # verified after download when set; see assets.download

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
        return self.local_filename or os.path.basename(self.filename)

    def all_names(self) -> set[str]:
        """Every basename this spec puts under data/models/ — the prune claim."""
        return {
            self.local_name,
            *(v.local_name for v in self.variants),
            *(f.local_name for f in self.extra_files),
        }


# The two prose-rewriter repos, pinned. Named once because three variants
# share them and a half-updated pin is a silently different model. The two
# lines version independently — upstream releases the sizes on their own
# cadence, so a mismatched pair of version numbers here is not a typo.
_PROSE_1_7B_REPO = "chartreuse-verte/prose-rewriter-1.7b-v2"
_PROSE_1_7B_REV = "e5c1b8282385365ba031a5966aec7f1fc0ef2fc1"
_PROSE_4B_REPO = "chartreuse-verte/prose-rewriter-4b-v2"
_PROSE_4B_REV = "33bcd356dfb172e8c7fa9d7427d24cd274230b9c"

# --- Spark-TTS, the built-in voice cloner -----------------------------------
_SPARK_LLM_REPO = "mradermacher/Spark-TTS-0.5B-GGUF"
_SPARK_LLM_REV = "5ba102cd2dfa63b55657d62cc2d216d97abbdc4b"
_SPARK_CODEC_REPO = "chartreuse-verte/Spark-TTS-0.5B-ONNX"
_SPARK_CODEC_REV = "4fa08a1c26784030ddd92d9cf2ab7a2efee3ffc4"
_SPARK_SPEAKER_REPO = _SPARK_CODEC_REPO
_SPARK_SPEAKER_REV = _SPARK_CODEC_REV

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
        repo_id="chartreuse-verte/ettin-povtense-17m-v2",
        filename="gguf/povtense-17m-q8_0.gguf",
        size_mb=20,
        revision="bacd633b181b7efdfbb9ba668c8c530ba47ad8a0",
        local_filename="povtense-17m-v2-q8_0.gguf",
    ),
    "markup_classifier": ModelSpec(
        repo_id="chartreuse-verte/ettin-markup-17m",
        filename="gguf/markup-17m-q8_0.gguf",
        size_mb=20,
        revision="758d5236405776dd801452a4954b047ba63775aa",
    ),
    # Not an in-process model: served by a child llama-server (see
    # local_models/llama_server/, driven by the Prose Rewriter workflow host).
    # `filename`/`size_mb` name the default variant so the legacy single-file
    # paths keep working; the selector reads `variants`, and every basename
    # here must also be claimed by prune_stale.
    "prose_rewriter": ModelSpec(
        repo_id=_PROSE_4B_REPO,
        filename="GGUF/prose-rewriter-4b-v2-Q8_0.gguf",
        size_mb=4694,
        revision=_PROSE_4B_REV,
        runtime="llama_server",
        variants=(
            ModelVariantSpec(
                id="1.7b-q8",
                label="1.7B · Q8_0",
                detail="Fastest, good enough.",
                repo_id=_PROSE_1_7B_REPO,
                path="GGUF/prose-rewriter-1.7b-v2-Q8_0.gguf",
                revision=_PROSE_1_7B_REV,
                size_mb=2165,
            ),
            ModelVariantSpec(
                id="4b-q4km",
                label="4B · Q4_K_M",
                detail="Medium quality.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v2-Q4_K_M.gguf",
                revision=_PROSE_4B_REV,
                size_mb=2716,
            ),
            ModelVariantSpec(
                id="4b-q8",
                label="4B · Q8_0",
                detail="Best quality, invents the least.",
                repo_id=_PROSE_4B_REPO,
                path="GGUF/prose-rewriter-4b-v2-Q8_0.gguf",
                revision=_PROSE_4B_REV,
                size_mb=4694,
            ),
        ),
    ),
    # The Spark-TTS half that runs on the GPU: a 0.5B Qwen2 whose vocabulary
    # carries 12 288 audio tokens alongside ordinary text. Driven by the same
    # child-process runtime as the prose rewriter, which is what lets Orb's
    # Vulkan build accelerate the 63% of synthesis wall time that lives here.
    "spark_tts_llm": ModelSpec(
        repo_id=_SPARK_LLM_REPO,
        filename="Spark-TTS-0.5B.Q8_0.gguf",
        size_mb=520,
        revision=_SPARK_LLM_REV,
        runtime="llama_server",
        sha256="9ea2db6a658652c7f5ad35d5c428ad476e37cd7c9a20fe3c9b1a0fbc26da6b0f",
    ),
    # The Spark-TTS half that runs on the CPU, as two ONNX graphs that must
    # travel together: `bicodec.onnx` turns audio tokens back into a waveform,
    # `spark-speaker-encoder.onnx` turns an uploaded clip into the 32 ints that
    # name a voice. Neither is a choice the user makes, so they are companions
    # rather than variants.
    "spark_tts_codec": ModelSpec(
        repo_id=_SPARK_CODEC_REPO,
        filename="bicodec.onnx",
        size_mb=368,
        revision=_SPARK_CODEC_REV,
        runtime="onnx",
        sha256="a1459483cf562e9de8b9d6077e9246b38df797b81d392b6c5a3cb78a595bb7df",
        extra_files=(
            ModelFileSpec(
                repo_id=_SPARK_SPEAKER_REPO,
                path="spark-speaker-encoder.onnx",
                revision=_SPARK_SPEAKER_REV,
                size_mb=23,
                sha256="808b0c09187fe031d896c5fdb92d6fc30fd9ac46f951b95988f6065d60ece22c",
            ),
        ),
    ),
}
