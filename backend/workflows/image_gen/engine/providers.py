"""Cloud image provider presets and the pure request builders that read them.

No I/O lives here -- every function below maps arguments to a value, so the wire
format of each provider is unit-testable without a network.

**The request builder is a strict allowlist.** xAI silently ignores unknown
fields, which means the API will never tell you a parameter was wrong: send
`negative_prompt` to a provider that has no such field and it returns a perfectly
good image that ignored it. "Send everything and let the server sort it out" is
the difference between a working negative prompt and a silently discarded one, so
a field is emitted only when the preset declares it.

Only the **xai** row is verified against the live API. Every other row is
declared from vendor documentation and marked ``verified=False``; this table is
the single place they live and the single place they get corrected.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import ResolvedReference

# How far a mapped aspect ratio may sit from the requested one before the user is
# told. ~2%: below that the difference is a few pixels of crop on a 1024px edge.
ASPECT_NOTE_THRESHOLD = 0.02


@dataclass(frozen=True)
class ProviderPreset:
    """One cloud image provider's wire dialect."""

    id: str
    label: str
    base_url: str
    generations_path: str = "/images/generations"
    edits_path: str = ""
    models_path: str = "/models"
    # OpenAI answers `{"data":[{id}]}`; xAI's image-model endpoint answers
    # `{"models":[{id, aliases, ...}]}`. Not the same shape, not interchangeable.
    models_response: str = "openai_data"
    # "size" -> `size: "1024x1024"`. "aspect_ratio" -> `aspect_ratio: "16:9"`.
    # "none" -> the provider decides. Never send the other spelling: xAI rejects
    # `size` outright ("Argument not supported: size"), which is the *polite*
    # failure mode -- the impolite one is accepting and ignoring it.
    dimension_mode: str = "none"
    aspect_ratios: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    supports_quality: bool = False
    supports_references: bool = False
    # The JSON field references ride in on the edits endpoint.
    reference_field: str = "images"
    reference_mimes: tuple[str, ...] = ("image/png", "image/jpeg")
    response_formats: tuple[str, ...] = ("b64_json", "url")
    default_model: str = ""
    max_prompt: int = 4_000
    docs_url: str = ""
    # False until someone has actually probed the live API and corrected this row.
    verified: bool = False
    # Permanent capability gaps, stated in the settings panel rather than as a
    # per-render note: "xAI has no negative prompt" is true of every image forever,
    # and a note that fires on 100% of renders is one users learn to ignore --
    # which then hides the per-render disclosures that matter.
    gaps: tuple[str, ...] = ()


_XAI_ASPECTS = (
    "1:1",
    "3:4",
    "4:3",
    "9:16",
    "16:9",
    "2:3",
    "3:2",
    "9:19.5",
    "19.5:9",
    "9:20",
    "20:9",
    "1:2",
    "2:1",
)

_OPENAI_SIZES = ("1024x1024", "1024x1536", "1536x1024")

_GAPS_NO_CONTROLS = (
    "ignores negative prompts, seed, steps and CFG",
    "applies style prompts and the resolution",
)

PRESETS: tuple[ProviderPreset, ...] = (
    # ── verified against the live API ────────────────────────────────────────
    ProviderPreset(
        id="xai",
        label="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        edits_path="/images/edits",
        # NOT the OpenAI `{"data": [...]}` shape.
        models_path="/image-generation-models",
        models_response="models_list",
        dimension_mode="aspect_ratio",
        aspect_ratios=_XAI_ASPECTS,
        supports_quality=True,
        supports_references=True,
        # Confirmed end-to-end: `images: [{"url": "data:image/png;base64,..."}]`
        # on a JSON body, not multipart.
        reference_field="images",
        default_model="grok-imagine-image",
        max_prompt=8_000,
        docs_url="https://docs.x.ai/docs/guides/image-generations",
        verified=True,
        gaps=_GAPS_NO_CONTROLS,
    ),
    # ── declared from vendor docs, unverified ────────────────────────────────
    ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        edits_path="/images/edits",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        supports_quality=True,
        supports_references=True,
        reference_field="image",
        default_model="gpt-image-1",
        docs_url="https://platform.openai.com/docs/api-reference/images",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        dimension_mode="none",
        docs_url="https://openrouter.ai/docs",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="togetherai",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        dimension_mode="size",
        sizes=("512x512", "768x768", "1024x1024"),
        supports_negative_prompt=True,
        supports_seed=True,
        default_model="black-forest-labs/FLUX.1-schnell",
        docs_url="https://docs.together.ai/reference/post-images-generations",
        gaps=("applies style prompts, the negative prompt, the seed and the resolution",),
    ),
    ProviderPreset(
        id="nanogpt",
        label="NanoGPT",
        base_url="https://nano-gpt.com/api/v1",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        docs_url="https://docs.nano-gpt.com/",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="chutes",
        label="Chutes",
        base_url="https://llm.chutes.ai/v1",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        docs_url="https://chutes.ai/",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="zai",
        label="Z.AI",
        base_url="https://api.z.ai/api/paas/v4",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        default_model="cogview-4",
        docs_url="https://docs.z.ai/",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="aimlapi",
        label="AI/ML API",
        base_url="https://api.aimlapi.com/v1",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        docs_url="https://docs.aimlapi.com/",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="electronhub",
        label="ElectronHub",
        base_url="https://api.electronhub.ai/v1",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        docs_url="https://docs.electronhub.ai/",
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="custom",
        label="Custom (OpenAI-compatible)",
        # Nothing to default to: the user supplies the base URL, and the config
        # normalizer is what refuses a credentialed or plaintext one.
        base_url="",
        edits_path="/images/edits",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        supports_references=True,
        reference_field="image",
        gaps=("is assumed to speak the OpenAI images API; capabilities are unknown",),
    ),
)

_BY_ID: dict[str, ProviderPreset] = {preset.id: preset for preset in PRESETS}


def get_preset(provider_id: str) -> ProviderPreset | None:
    return _BY_ID.get(provider_id)


def provider_catalogue() -> list[dict]:
    """The preset table projected for the settings panel.

    A projection, never the config: no configured `api_key` may enter this
    payload, and nothing here reads one.
    """
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "base_url": preset.base_url,
            "needs_base_url": not preset.base_url,
            "default_model": preset.default_model,
            "dimension_mode": preset.dimension_mode,
            "aspect_ratios": list(preset.aspect_ratios),
            "sizes": list(preset.sizes),
            "supports_negative_prompt": preset.supports_negative_prompt,
            "supports_seed": preset.supports_seed,
            "supports_quality": preset.supports_quality,
            "supports_references": preset.supports_references,
            "docs_url": preset.docs_url,
            "verified": preset.verified,
            "gaps": list(preset.gaps),
        }
        for preset in PRESETS
    ]


# ── dimensions ───────────────────────────────────────────────────────────────


def _parse_ratio(candidate: str) -> float | None:
    left, _, right = candidate.partition(":")
    try:
        numerator, denominator = float(left), float(right)
    except ValueError:
        return None
    return numerator / denominator if denominator else None


def aspect_for(preset: ProviderPreset, width: int, height: int) -> tuple[str, str | None]:
    """The declared aspect ratio nearest to `width`x`height`, and any disclosure.

    Nearest by ``|log(target) - log(candidate)|`` rather than by raw difference,
    so 2:1 and 1:2 are equally far from 1:1 -- a linear metric would treat
    "twice as wide" as four times the error of "twice as tall".
    """
    if not preset.aspect_ratios or width <= 0 or height <= 0:
        return "", None
    target = width / height
    best = min(
        (candidate for candidate in preset.aspect_ratios if _parse_ratio(candidate)),
        key=lambda candidate: abs(math.log(target) - math.log(_parse_ratio(candidate) or 1.0)),
        default="",
    )
    if not best:
        return "", None
    chosen = _parse_ratio(best) or target
    error = abs(chosen - target) / target
    if error <= ASPECT_NOTE_THRESHOLD:
        return best, None
    return best, f"{preset.label} renders fixed aspect ratios; {width}x{height} was rendered as {best}"


def size_for(preset: ProviderPreset, width: int, height: int) -> tuple[str, str | None]:
    """The declared `size` string nearest to `width`x`height`, and any disclosure."""
    requested = f"{width}x{height}"
    if not preset.sizes or requested in preset.sizes:
        return (requested if preset.sizes or preset.dimension_mode == "size" else ""), None
    target = width / height if height else 1.0

    def distance(candidate: str) -> tuple[float, float]:
        cw, _, ch = candidate.partition("x")
        try:
            candidate_w, candidate_h = int(cw), int(ch)
        except ValueError:
            return (math.inf, math.inf)
        ratio = candidate_w / candidate_h if candidate_h else 1.0
        return (abs(math.log(target) - math.log(ratio)), abs(candidate_w * candidate_h - width * height))

    best = min(preset.sizes, key=distance)
    return best, f"{preset.label} accepts fixed sizes; {requested} was rendered as {best}"


# ── request builders ─────────────────────────────────────────────────────────


@dataclass
class BuiltRequest:
    """A body plus whatever the caller must disclose about building it."""

    body: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def _dimension_fields(preset: ProviderPreset, width: int | None, height: int | None) -> BuiltRequest:
    if width is None or height is None or preset.dimension_mode == "none":
        return BuiltRequest({})
    if preset.dimension_mode == "aspect_ratio":
        ratio, note = aspect_for(preset, width, height)
        return BuiltRequest({"aspect_ratio": ratio} if ratio else {}, [note] if note else [])
    if preset.dimension_mode == "size":
        size, note = size_for(preset, width, height)
        return BuiltRequest({"size": size} if size else {}, [note] if note else [])
    return BuiltRequest({})


def _prompt_field(preset: ProviderPreset, prompt: str) -> BuiltRequest:
    if len(prompt) <= preset.max_prompt:
        return BuiltRequest({"prompt": prompt})
    return BuiltRequest(
        {"prompt": prompt[: preset.max_prompt]},
        [f"the prompt was truncated to {preset.max_prompt} characters, which is {preset.label}'s limit"],
    )


def build_generation_body(
    preset: ProviderPreset,
    *,
    model: str,
    prompt: str,
    negative_prompt: str = "",
    width: int | None = None,
    height: int | None = None,
    quality: str = "",
    seed: int | None = None,
    n: int = 1,
    response_format: str = "",
) -> BuiltRequest:
    """The `POST /images/generations` body, as a strict allowlist.

    Deliberately absent, on every provider:

    * ``moderation`` -- team-gated on xAI; sending it hard-fails the whole call
      with `permission-denied`.
    * ``user`` -- a stable identifier shipped to a third party for no benefit here.
    * ``style`` -- xAI takes a free string, but Orb styles already inject prompt
      text, and doing both double-applies the style.
    """
    built = _prompt_field(preset, prompt)
    body: dict[str, Any] = {"model": model, **built.body, "n": n}
    notes = list(built.notes)

    dimensions = _dimension_fields(preset, width, height)
    body.update(dimensions.body)
    notes.extend(dimensions.notes)

    if preset.supports_negative_prompt and negative_prompt.strip():
        body["negative_prompt"] = negative_prompt
    if preset.supports_seed and seed is not None:
        body["seed"] = seed
    if preset.supports_quality and quality:
        body["quality"] = quality
    fmt = response_format or (preset.response_formats[0] if preset.response_formats else "")
    if fmt:
        body["response_format"] = fmt
    return BuiltRequest(body, notes)


def _data_uri(reference: ResolvedReference) -> str:
    import base64

    return f"data:{reference.mime};base64,{base64.b64encode(reference.data).decode('ascii')}"


def build_edit_body(
    preset: ProviderPreset,
    *,
    model: str,
    prompt: str,
    references: Sequence[ResolvedReference],
    negative_prompt: str = "",
    seed: int | None = None,
    width: int | None = None,
    height: int | None = None,
    quality: str = "",
    n: int = 1,
    response_format: str = "",
) -> BuiltRequest:
    """The `POST /images/edits` body. JSON, not multipart -- verified on xAI.

    References travel as `data:` URIs so nothing has to be uploaded first and no
    third party is handed a fetchable URL into Orb.

    The same allowlist as the generation body, plus the references: a provider
    that declares a negative prompt or a seed honours them on this endpoint too,
    and one that does not still receives neither.
    """
    built = build_generation_body(
        preset,
        model=model,
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        width=width,
        height=height,
        quality=quality,
        n=n,
        response_format=response_format,
    )
    body = built.body
    uris = [_data_uri(reference) for reference in references]
    if preset.reference_field == "image":
        # The singular spelling takes one object, so extra references would be
        # silently dropped -- say so rather than let the user wonder which one won.
        body["image"] = {"url": uris[0]} if uris else None
        if len(uris) > 1:
            built.notes.append(f"{preset.label} accepts one reference image; only the first was sent")
    else:
        body[preset.reference_field] = [{"url": uri} for uri in uris]
    return built
