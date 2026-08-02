"""Cloud image provider presets and the pure request builders that read them.

No I/O -- every function below maps arguments to a value, so each provider's wire
format is unit-testable without a network.

**The request builder is a strict allowlist.** xAI silently ignores unknown fields,
so the API never tells you a parameter was wrong: send `negative_prompt` to a
provider that has no such field and it returns a perfectly good image that ignored
it. A field is emitted only when the preset declares it.

Only the **xai**, **togetherai** and **nanogpt** rows are verified against the live
API; every other row is declared from vendor documentation and marked
``verified=False``.

Together is the cautionary tale the paragraph above describes. It was declared
``dimension_mode="size"`` from its docs; the live API accepts `size`, ignores it,
and renders the model default -- so a 1024x576 request was mapped to a square,
disclosed to the user as a square, and came back 1024x768. Three separate wrong
answers from one undeclared field. It speaks `width`/`height` integers instead.

NanoGPT is the second: its documented `/v1/models` holds 653 models and **not one
of them makes an image**, so the row Orb shipped from the docs would have filled the
picker with chat models and offered no way to reach the 202 image ones. Its image
catalogue is a different path, off the version prefix, in a third response shape.
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
    # Joined onto `base_url`, so `../` reaches off the version prefix: NanoGPT's
    # image catalogue lives at `/api/models` while everything else it serves is
    # under `/api/v1`.
    models_path: str = "/models"
    # OpenAI answers `{"data":[{id}]}`; xAI's image-model endpoint answers
    # `{"models":[{id, ...}]}`; Together answers a *bare JSON array*; NanoGPT answers
    # `{"models": {"image": {id: {...}}}}` -- a mapping keyed by id, not a list at
    # all. Not the same shape, not interchangeable -- reading the wrong one yields an
    # empty list and a Test connection button that fails against a healthy provider.
    models_response: str = "openai_data"
    # A free, *authenticated* GET that proves the key works. Only declared where the
    # model list cannot: NanoGPT serves its catalogue to anonymous callers, so a Test
    # connection resting on it answers "Connected" to a key that cannot render a
    # thing. Never a generations path -- a Test button that bills is unacceptable.
    auth_probe_path: str = ""
    # Kept only when an entry declares this `type`. Together's `/models` is one list
    # of everything it hosts -- 271 entries, 29 of them image models -- so without
    # this the picker is 242 chat models the user has to scroll past.
    models_type_filter: str = ""
    # "size" -> `size: "1024x1024"`, "aspect_ratio" -> `aspect_ratio: "16:9"`,
    # "width_height" -> `width: 1024, height: 576`, "none" -> the provider decides.
    # Never send the other spelling: xAI rejects `size` outright, which is the
    # *polite* failure -- the impolite one is Together accepting and ignoring it.
    dimension_mode: str = "none"
    aspect_ratios: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    # `width_height` only: the per-edge pixel bounds and the granularity the provider
    # accepts. Together 400s on a non-multiple of 16, so this is a hard contract
    # rather than a rounding preference.
    min_dimension: int = 0
    max_dimension: int = 0
    dimension_step: int = 0
    supports_negative_prompt: bool = False
    # Models that accept `negative_prompt` and silently drop it -- matched as
    # lowercase substrings of the model id. A provider-level capability with a
    # model-level hole: FLUX.1-schnell is distilled and runs without CFG, so it has
    # nothing to apply a negative prompt *with*, and says so by returning the
    # byte-identical image. Verified per entry, never inferred from a family name.
    negative_prompt_blind: tuple[str, ...] = ()
    supports_seed: bool = False
    supports_quality: bool = False
    supports_references: bool = False
    # The JSON field references ride in. On a provider with no `edits_path` they
    # ride the generations body instead -- Together has no `/images/edits` at all,
    # yet its edit models take an `image_url` on the ordinary generations call, so
    # "no edits endpoint" and "no reference support" are not the same fact.
    reference_field: str = "images"
    # How that field is shaped: "url_objects" -> `[{"url": ...}]`, "url_object" ->
    # `{"url": ...}`, "string" -> the bare URI. Declared rather than inferred from
    # the field name, because getting it wrong is invisible: Together answers 200
    # and renders the prompt alone when the shape is not the one it wanted.
    reference_encoding: str = "url_objects"
    # When non-empty, only these models accept a reference -- same lowercase
    # substring match, and the same epistemics as `negative_prompt_blind`: an
    # allowlist, so a model nobody has probed is treated as incapable. Under-
    # promising costs a disclosure; over-promising costs a silent no-op that
    # renders without the character reference and still bills for it.
    reference_models: tuple[str, ...] = ()
    # True when a reference render takes its size from the reference rather than
    # from the request. An image-to-image model generally follows its input, so the
    # resolution picker silently stops applying the moment references are on.
    reference_drives_size: bool = False
    reference_mimes: tuple[str, ...] = ("image/png", "image/jpeg")
    response_formats: tuple[str, ...] = ("b64_json", "url")
    default_model: str = ""
    max_prompt: int = 4_000
    docs_url: str = ""
    # False until someone has probed the live API and corrected this row.
    verified: bool = False
    # Permanent capability gaps, stated in the settings panel rather than as a
    # per-render note: a note that fires on 100% of renders is one users learn to
    # ignore, which then hides the per-render disclosures that matter.
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
        # Confirmed end-to-end: a JSON body, not multipart.
        reference_field="images",
        default_model="grok-imagine-image",
        max_prompt=8_000,
        docs_url="https://docs.x.ai/docs/guides/image-generations",
        verified=True,
        gaps=_GAPS_NO_CONTROLS,
    ),
    ProviderPreset(
        id="togetherai",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        # A bare JSON array, and one catalogue for every modality it hosts.
        models_response="bare_list",
        models_type_filter="image",
        # NOT `size`, whatever the OpenAI-compatible framing suggests: see the
        # module docstring. Verified end to end -- 1024x576 in, 1024x576 out.
        dimension_mode="width_height",
        min_dimension=64,
        max_dimension=1792,
        dimension_step=16,
        supports_negative_prompt=True,
        # Distilled models run without CFG, so they have nothing to apply a negative
        # prompt *with* and return the byte-identical image. Only ids actually probed
        # this way are listed: on a provider that reproduces a seed inconsistently,
        # identical bytes prove the field was dropped but differing bytes prove
        # nothing, so "not listed here" means unverified, never "known to work".
        negative_prompt_blind=("flux.1-schnell", "juggernaut-lightning"),
        supports_seed=True,
        # No `/images/edits` -- the reference rides the generations body. Verified
        # live on FLUX.1-kontext pro *and* max: a `data:` URI in `image_url`
        # reproduces the reference frame, while `image`, `images` and `image_urls`
        # are accepted and ignored.
        #
        # Off the allowlist the provider is inconsistent, which is exactly why the
        # model decides whether a slot is offered at all: FLUX.2 and Seedream answer
        # *"Unsupported use of 'image_url' parameter"*, but the FLUX.1-schnell
        # default answers 200 and renders the prompt alone -- a paid render that
        # silently drops the character reference.
        supports_references=True,
        reference_field="image_url",
        reference_encoding="string",
        reference_models=("kontext",),
        # Kontext derives the output size from the reference: a 512x512 reference on
        # a 1024x576 request came back 1024x1024. The picker cannot win that, so the
        # render says so instead of quietly handing back a different shape.
        reference_drives_size=True,
        default_model="black-forest-labs/FLUX.1-schnell",
        docs_url="https://docs.together.ai/reference/post-images-generations",
        verified=True,
        gaps=(
            "applies style prompts and the resolution",
            "honours a seed, though the same seed does not always reproduce the same image",
            "reports no cost for a render",
        ),
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
        reference_encoding="url_object",
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
        id="nanogpt",
        label="NanoGPT",
        base_url="https://nano-gpt.com/api/v1",
        edits_path="/images/edits",
        # NOT `/v1/models`. That path answers 200 with 653 models and not one of them
        # makes an image -- the image catalogue is 202 separate entries one level up,
        # under `models.image`, keyed by id rather than listed.
        models_path="../models",
        models_response="nanogpt_image_map",
        # `/v1/models` answers 200 to a bogus key *and* to no key at all, so a Test
        # connection resting on it is not a test. `/v1/usage` is free and 401s.
        auth_probe_path="/usage",
        # Verified: `size: "1024x576"` is understood whatever vocabulary the chosen
        # model publishes -- NanoGPT translates it, so an aspect-ratio model answered
        # 1344x768 and a named-size model answered 1024x576. No `sizes` menu, because
        # there is no provider-wide one to declare: each model publishes its own, and
        # snapping the request to a menu the *next* model does not share would be a
        # worse answer than the one the provider itself picks. See `gaps`.
        dimension_mode="size",
        supports_negative_prompt=True,
        # Verified by rendering one seed with and without a contradicting negative
        # prompt: byte-identical means the field was dropped. Sound here only because
        # the same seed reproduces on this provider -- a third call proves that per
        # model before the comparison is trusted, which is what stopped `pixelwave`
        # from being listed on a run where two identical requests disagreed.
        negative_prompt_blind=("hidream-i1",),
        supports_seed=True,
        # Verified live on `step-image-edit-2`: `image` as a bare `data:` URI, on
        # either path. `images: [{"url": ...}]` -- the shape xAI and OpenAI want --
        # is the one spelling NanoGPT rejects outright, with `missing_image_input`.
        supports_references=True,
        reference_field="image",
        reference_encoding="string",
        # Deliberately empty: 118 of the 202 image models take a reference, the model
        # id is in front of the user when they choose it, and an allowlist over a
        # catalogue this size is a maintenance burden that silently narrows as
        # NanoGPT adds models. The `gaps` line states the trade instead.
        default_model="cyberrealistic-xl",
        # Verified: 3000, and it 400s rather than truncating.
        max_prompt=3_000,
        docs_url="https://docs.nano-gpt.com/",
        verified=True,
        gaps=(
            "applies style prompts, the resolution, negative prompts and a seed",
            "renders the nearest size its model supports, which may not be the one requested",
            "accepts reference images on some models only; the rest bill for the render and ignore them",
            "reports the cost of each render in USD",
        ),
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
        reference_encoding="url_object",
        gaps=("is assumed to speak the OpenAI images API; capabilities are unknown",),
    ),
)

_BY_ID: dict[str, ProviderPreset] = {preset.id: preset for preset in PRESETS}


def get_preset(provider_id: str) -> ProviderPreset | None:
    return _BY_ID.get(provider_id)


def provider_catalogue() -> list[dict]:
    """The preset table projected for the settings panel. A projection, never the
    config: no configured `api_key` may enter this payload."""
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
            # Empty means "every model on this provider takes them"; non-empty is the
            # allowlist the panel matches the chosen model against.
            "reference_models": list(preset.reference_models),
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

    Nearest in log space, so 2:1 and 1:2 are equally far from 1:1 -- a linear metric
    would call "twice as wide" four times the error of "twice as tall".
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
    """The declared `size` string nearest to `width`x`height`, and any disclosure.

    Reached only for a `size` provider, so a preset that declares no menu is taken
    to accept the request verbatim rather than being sent nothing.
    """
    requested = f"{width}x{height}"
    if not preset.sizes or requested in preset.sizes:
        return requested, None
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


def pixels_for(preset: ProviderPreset, width: int, height: int) -> tuple[int, int, str | None]:
    """`width`x`height` snapped to what the provider's pixel grid accepts.

    Unlike `size_for` this is not a menu, so the requested aspect ratio survives:
    an over-large request is scaled down whole (both edges, one factor) rather than
    clamped per edge, which would turn 2560x1440 into a 1792x1440 near-square.
    Snapping to the step comes second, and can only move an edge by <`step` pixels.
    """
    step = preset.dimension_step or 1
    low, high = preset.min_dimension or step, preset.max_dimension or 0
    requested = f"{width}x{height}"

    scale = min(high / width, high / height, 1.0) if high else 1.0
    scaled_w, scaled_h = width * scale, height * scale

    def snap(value: float) -> int:
        stepped = int(round(value / step)) * step
        # Rounding a 70px edge down to 64 is fine; rounding it to 0 is not, and the
        # provider would answer with a 400 rather than an image.
        stepped = max(stepped, low)
        return min(stepped, high) if high else stepped

    final_w, final_h = snap(scaled_w), snap(scaled_h)
    if (final_w, final_h) == (width, height):
        return final_w, final_h, None
    return (
        final_w,
        final_h,
        f"{preset.label} renders in steps of {step}px up to {high}px; {requested} was rendered as {final_w}x{final_h}",
    )


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
    if preset.dimension_mode == "width_height":
        final_w, final_h, note = pixels_for(preset, width, height)
        return BuiltRequest({"width": final_w, "height": final_h}, [note] if note else [])
    return BuiltRequest({})


def drops_negative_prompt(preset: ProviderPreset, model: str) -> bool:
    """True when this model accepts `negative_prompt` and does nothing with it."""
    lowered = model.lower()
    return any(marker in lowered for marker in preset.negative_prompt_blind)


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
) -> BuiltRequest:
    """The `POST /images/generations` body, as a strict allowlist.

    Deliberately absent on every provider: ``moderation`` (team-gated on xAI, and
    sending it hard-fails the call), ``user`` (a stable identifier shipped to a third
    party for no benefit), and ``style`` (Orb styles already inject prompt text, so
    sending both double-applies it).
    """
    built = _prompt_field(preset, prompt)
    body: dict[str, Any] = {"model": model, **built.body, "n": n}
    notes = list(built.notes)

    dimensions = _dimension_fields(preset, width, height)
    body.update(dimensions.body)
    notes.extend(dimensions.notes)

    if preset.supports_negative_prompt and negative_prompt.strip():
        # Still sent: support is a provider-level fact, and a model that ignores the
        # field today is one the provider may teach it tomorrow. What changes is that
        # the user is told, at the render that discarded it, rather than watching a
        # negative style prompt quietly do nothing.
        body["negative_prompt"] = negative_prompt
        if drops_negative_prompt(preset, model):
            notes.append(f"{model} ignores negative prompts, so the negative style prompt had no effect")
    if preset.supports_seed and seed is not None:
        body["seed"] = seed
    if preset.supports_quality and quality:
        body["quality"] = quality
    if preset.response_formats:
        # The preset's first choice: `b64_json` everywhere today, which is one
        # fewer hop and nothing fetching an attacker-influenceable URL.
        body["response_format"] = preset.response_formats[0]
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
) -> BuiltRequest:
    """The `POST /images/edits` body. JSON, not multipart -- verified on xAI.

    References travel as `data:` URIs, so nothing is uploaded first and no third
    party is handed a fetchable URL into Orb. The same allowlist as the generation
    body, plus the references.
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
    )
    body = built.body
    uris = [_data_uri(reference) for reference in references]
    if preset.reference_encoding == "url_objects":
        body[preset.reference_field] = [{"url": uri} for uri in uris]
    elif preset.reference_encoding == "string":
        body[preset.reference_field] = uris[0] if uris else None
    else:
        body[preset.reference_field] = {"url": uris[0]} if uris else None
    # Only the list encoding carries more than one, so anywhere else the extras are
    # dropped -- say so rather than let the user wonder which one won.
    if len(uris) > 1 and preset.reference_encoding != "url_objects":
        built.notes.append(f"{preset.label} accepts one reference image; only the first was sent")
    if uris and preset.reference_drives_size:
        # Verified on Kontext: a 512x512 reference on a 1024x576 request came back
        # 1024x1024. Disclosed rather than left to be noticed, because the picker
        # still shows the resolution that no longer applies.
        built.notes.append("the reference image set the output size, so the resolution setting did not apply")
    return built


def takes_references(preset: ProviderPreset, model: str) -> bool:
    """True when this model is verified to accept a reference image.

    A provider whose whole catalogue takes them declares no `reference_models` and
    every model passes; one where it is the exception names the exceptions.
    """
    if not preset.supports_references:
        return False
    if not preset.reference_models:
        return True
    lowered = model.lower()
    return any(marker in lowered for marker in preset.reference_models)
