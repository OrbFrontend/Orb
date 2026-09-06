"""Define cloud image provider presets and request builders."""

from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import ResolvedReference, ratio_distance

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
    # thing. Never a generations path -- see `validate_connection` on ImageAdapter.
    auth_probe_path: str = ""
    # How a catalogue entry is recognised as an image model, naming a rule in
    # `_MODEL_FILTERS` (openai_image_client.py); "" keeps every entry. Needed
    # wherever `/models` is one list of everything the provider hosts: Together's is
    # 271 entries, 29 of them image models, so without this the picker is 242 chat
    # models the user has to scroll past. Which *field* answers the question is not
    # agreed on -- Together tags each entry `type: "image"`, while OpenRouter has no
    # `type` field at all and declares `architecture.output_modalities` instead --
    # so the rule is declared per provider rather than inferred from the payload.
    models_filter: str = ""
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
    # model-level hole: a distilled model runs without CFG, so it has nothing to
    # apply a negative prompt *with*, and says so by returning the byte-identical
    # image for one seed with and without one.
    #
    # That comparison is only sound where the seed reproduces, which a third call
    # proves per model first -- it is what kept `pixelwave` off the NanoGPT row when
    # two identical requests disagreed. So an id not listed here is one nobody
    # probed, never one known to work, and never a family name inferred from a peer.
    negative_prompt_blind: tuple[str, ...] = ()
    supports_seed: bool = False
    supports_quality: bool = False
    supports_references: bool = False
    # The JSON field references ride in. On a provider with no `edits_path` they
    # ride the generations body instead -- Together has no `/images/edits` at all,
    # yet its edit models take an `image_url` on the ordinary generations call, so
    # "no edits endpoint" and "no reference support" are not the same fact.
    reference_field: str = "images"
    # How that field is shaped: "url_objects" -> `[{"url": ...}]`, "image_url_objects"
    # -> `[{"image_url": ...}]`, "string_list" -> `["<uri>", ...]`, "url_object" ->
    # `{"url": ...}`, "string" -> the bare URI. Declared rather than inferred from the
    # field name, because getting it wrong is invisible: Together answers 200 and
    # renders the prompt alone when the shape is not the one it wanted. Nor is the
    # *field* name enough to pin the element: OpenAI takes `images` like xAI and then
    # rejects xAI's `{"url": ...}` element, wanting `{"image_url": "<uri>"}` -- one key
    # deeper than the field name suggests.
    #
    # This is the one genuinely irreducible per-provider fact about references, and it
    # is also what decides **capacity**: a list encoding can carry as many as the
    # picker allows, a scalar one carries exactly one. Nothing else needs measuring.
    reference_encoding: str = "url_objects"
    # Deliberately absent: a per-model reference allowlist. It was a hand-measured table
    # over catalogues that grow without us -- NanoGPT alone ships 202 image models -- so
    # it was permanently unfinished, and an unfinished allowlist silently withholds a
    # capability the user is paying for. A model that will not take what it was sent says
    # so itself: `engine/degrade.py` reads the refusal and re-renders without it.
    # Rejections are free on every provider measured, so asking costs latency, not
    # money.
    # True when a reference render takes its size from the reference rather than
    # from the request. An image-to-image model generally follows its input, so the
    # resolution picker silently stops applying the moment references are on.
    reference_drives_size: bool = False
    reference_mimes: tuple[str, ...] = ("image/png", "image/jpeg")
    # The first entry is sent as `response_format`. **Empty means send no such field**,
    # which is not a preference but a hard contract on OpenAI: `gpt-image-1` answers
    # `unknown_parameter` to `response_format` on both paths and always returns
    # `b64_json` regardless, so declaring the format Orb wanted rejected every render.
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
        # Measured 2026-08-19: four references whose colours the prompt never named came
        # back as four objects, and swapping the array swapped the output left-to-right,
        # so `images` is positional rather than a bag. Recorded because it is the
        # evidence a list encoding carries more than its first element at all.
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
        models_filter="type_image",
        # NOT `size`, whatever the OpenAI-compatible framing suggests: see the
        # module docstring. Verified end to end -- 1024x576 in, 1024x576 out.
        dimension_mode="width_height",
        min_dimension=64,
        max_dimension=1792,
        dimension_step=16,
        supports_negative_prompt=True,
        negative_prompt_blind=("flux.1-schnell", "juggernaut-lightning"),
        supports_seed=True,
        # No `/images/edits` -- the reference rides the generations body. Verified on
        # FLUX.1-kontext pro *and* max: a `data:` URI in `image_url` reproduces the
        # reference frame, while `image`, `images` and `image_urls` are accepted and
        # ignored. Off the allowlist the provider is inconsistent, which is why the
        # model decides whether a slot is offered at all: FLUX.2 and Seedream answer
        # *"Unsupported use of 'image_url' parameter"*, but FLUX.1-schnell answered 200
        # and rendered the prompt alone.
        supports_references=True,
        reference_field="image_url",
        reference_encoding="string",
        # Kontext derives the output size from the reference: a 512x512 reference on
        # a 1024x576 request came back 1024x1024. The picker cannot win that, so the
        # render says so instead of quietly handing back a different shape.
        reference_drives_size=True,
        # Keep the default on a currently serverless model.
        default_model="black-forest-labs/FLUX.2-dev",
        docs_url="https://docs.together.ai/reference/post-images-generations",
        verified=True,
        gaps=(
            "applies style prompts and the resolution",
            "honours a seed, though the same seed does not always reproduce the same image",
            "reports no cost for a render",
        ),
    ),
    ProviderPreset(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        # No `edits_path`: `/images/edits` answers a plain-text "404 Not Found" --
        # not even JSON. Nor does the generations body take one; see below.
        models_response="openai_data",
        # 337 entries and not one `type` field. 11 declare an image output modality,
        # two of which are the `openrouter/auto` routers -- they 404 with "No
        # endpoint found" on this path, and are deliberately left in the picker
        # rather than denylisted: the provider's own words explain it, and a
        # hand-maintained exception list rots against a catalogue this size.
        models_filter="output_image",
        # `/models` answers 200 with no key at all, so the list proves the provider
        # is up and nothing about the key. `/key` is free, and 401s.
        auth_probe_path="/key",
        # Verified: `size` is read, then snapped by the model to its own vocabulary
        # -- "1024x576" came back 1344x768 and "576x1024" came back 768x1344. Hence
        # no `sizes` menu, for NanoGPT's reason: each model publishes its own, and
        # snapping to a menu the next model does not share is a worse answer than
        # the one the provider itself picks. See `gaps`.
        dimension_mode="size",
        # Everything else is left at its default-off, and each one was measured
        # rather than read off the catalogue:
        #
        # `supports_seed`: the image models all list `seed` in `supported_parameters`
        # -- that is the *chat* schema, and this endpoint is a shim over it. Two
        # calls at one seed returned different images on `gemini-2.5-flash-image`
        # *and* on `gpt-5-image-mini`, so it is a provider fact, not a per-model
        # hole, and `seed_honored` would be a claim the user cannot check.
        #
        # `supports_references`: the image models declare `image` among their input
        # modalities -- true of `/chat/completions`, not of this path. `image`,
        # `images` and `image_url` were each sent with an unmistakable reference
        # (magenta field, black circle) and a keep-the-background prompt; all three
        # answered 200 having rendered the prompt alone. Unknown fields are accepted
        # silently here, so "no error" is never evidence a field was read.
        default_model="google/gemini-2.5-flash-image",
        # Verified: 52,812 characters accepted. The real wall is the chosen model's
        # context, which is far past anything Orb assembles, so this is headroom.
        max_prompt=32_000,
        docs_url="https://openrouter.ai/docs/features/multimodal/image-generation",
        verified=True,
        gaps=(
            *_GAPS_NO_CONTROLS,
            "renders the nearest size its model supports, which may not be the one requested",
            "accepts no reference images on any model, whatever its catalogue entry claims",
            "reports the cost of each render in USD",
        ),
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        edits_path="/images/edits",
        # `/models` is 125 entries of every modality OpenAI hosts, carrying only
        # `id`, `object`, `created` and `owned_by` -- no `type` like Together, no
        # `architecture` like OpenRouter. Nothing in the payload answers "does this
        # make images", so the rule reads the id, which is the one field there is.
        models_filter="openai_image_ids",
        # Verified: `/models` 401s on a bad key, so the list already proves it and no
        # `auth_probe_path` is needed.
        dimension_mode="size",
        # Verified on gpt-image-1, -1-mini and -1.5, which name their own menu in the
        # rejection: "Supported sizes are 1024x1024, 1024x1536, 1536x1024, and auto."
        # `gpt-image-2` is the hole -- see `gaps`.
        sizes=_OPENAI_SIZES,
        # Verified: 'low', 'medium', 'high' and 'auto', which is CLOUD_QUALITIES plus
        # the default. dall-e-3's 'hd'/'standard' spelling is rejected -- and no
        # dall-e model appears in the catalogue at all any more.
        supports_quality=True,
        # Verified end to end on `/images/edits`: JSON, not multipart, and the magenta
        # reference came back magenta with `input_tokens_details.image_tokens: 194`
        # confirming it was read rather than accepted and dropped.
        #
        # Every reachable model takes one, measured rather than assumed: all five were
        # posted a reference alongside a deliberately invalid `size`, and each got as far
        # as its own size check, which is only reached once the model and the field have
        # been accepted. (`chatgpt-image-latest` 403s on org verification before any of
        # it, which the refusal ladder degrades on like any other.)
        supports_references=True,
        # NOT `image: {"url": ...}`. The field is `images` (an array) and its element
        # is `{"image_url": "<data uri>"}`; every other spelling is rejected by name,
        # which is the polite failure -- `images: [{"url": ...}]` even survives schema
        # validation against a bogus model and is only rejected once a real one is
        # named, so a probe that stops at "no error" reads it as accepted.
        reference_field="images",
        reference_encoding="image_url_objects",
        default_model="gpt-image-1",
        # Verified: 31,992 characters accepted, 39,996 rejected with "maximum length
        # 32000". The 4,000 default was dall-e-3's limit and truncated 28,000
        # characters of prompt that this API takes.
        max_prompt=32_000,
        # Verified: rejected outright as `unknown_parameter` on both paths.
        response_formats=(),
        docs_url="https://platform.openai.com/docs/api-reference/images",
        verified=True,
        gaps=(
            *_GAPS_NO_CONTROLS,
            "reports tokens rather than a cost, so no cost is shown for a render",
            "renders gpt-image-2 at the nearest of the three sizes its older models accept",
            "may need the organization verified before a model will render",
        ),
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
        # model publishes -- an aspect-ratio model answered 1344x768 and a named-size
        # model answered 1024x576. Hence no `sizes` menu: each model publishes its
        # own, and snapping to a menu the *next* model does not share is a worse
        # answer than the one the provider itself picks. See `gaps`.
        dimension_mode="size",
        supports_negative_prompt=True,
        negative_prompt_blind=("hidream-i1",),
        supports_seed=True,
        # Verified live on `step-image-edit-2`: `image` as a bare `data:` URI, on
        # either path. `images: [{"url": ...}]` -- the shape xAI and OpenAI want --
        # is the one spelling NanoGPT rejects outright, with `missing_image_input`.
        supports_references=True,
        # NOT `image`, which is single-only. Measured 2026-08-19: `imageDataUrls` is a
        # bare-string array that carries four references onto `nano-banana` in order,
        # and takes a *single* element just as happily -- so it replaces `image` outright
        # rather than adding a second code path for the multi case. Over-capacity is
        # refused by name (`IMAGE_INPUT_TOO_MANY`, quoting the model's limit) and the
        # refusal is free, which is what makes asking cheaper than tabulating: capacity
        # here is genuinely per-model, from 1 to 14 across the catalogue.
        reference_field="imageDataUrls",
        reference_encoding="string_list",
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
    # Dropped for having no JSON reference field, recorded so nobody re-adds them from
    # the same docs: **Chutes** has no OpenAI-shaped images endpoint at all -- `/v1` is
    # chat, `/images/` is *container* images, and its image models answer per-chute at
    # `{user}-{slug}.chutes.ai/generate`; **Z.AI** documents `model`, `prompt`,
    # `quality`, `size`, `user_id` and no edits path; **ElectronHub** has
    # `/images/edits`, but multipart with a PNG file part, over a text-to-image-only
    # generations body. Each is a row again the day it ships a JSON reference field.
    ProviderPreset(
        id="aimlapi",
        label="AI/ML API",
        base_url="https://api.aimlapi.com/v1",
        dimension_mode="size",
        sizes=_OPENAI_SIZES,
        # No `edits_path`: `/images/edits` here is multipart taking a local file, so the
        # reference rides the generations body as on Together. The field is documented
        # as "a list of URLs or local Base64 encoded images" -- unverified like the rest
        # of the row, so nobody has watched a `data:` URI come back in an image.
        supports_references=True,
        reference_field="image_url",
        reference_encoding="string",
        docs_url="https://docs.aimlapi.com/api-references/image-models",
        gaps=(
            *_GAPS_NO_CONTROLS,
            "accepts reference images on its image-to-image models only; the rest bill for the render and ignore them",
        ),
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


# What the settings panel is told about a provider. An allowlist, so a field added
# to the table above reaches the frontend only when someone puts it here -- which is
# also what keeps a credential from ever riding along, since none is named.
#
# The whole dimension contract rides along -- `sizes` for a menu provider, the
# min/max/step grid for a `width_height` one -- because the panel's resolution menu
# is otherwise a guess at what `size_for`/`pixels_for` will accept, and a guess there
# offers sizes that are silently snapped to something else at render time.
# `reference_drives_size` for the same reason: it is the one capability that decides
# whether that menu applies *at all*, and only the panel can say so before the bill.
_CATALOGUE_FIELDS = (
    "id label base_url default_model dimension_mode aspect_ratios sizes docs_url verified gaps "
    "min_dimension max_dimension dimension_step reference_drives_size "
    "supports_negative_prompt supports_seed supports_quality supports_references"
).split()


def provider_catalogue(ceiling: int) -> list[dict]:
    """The preset table projected for the settings panel. A projection, never the
    config: no configured `api_key` may enter this payload.

    `ceiling` is `MAX_REFERENCE_SLOTS`, passed rather than imported so this module
    stays free of the config layer it is read by."""
    return [
        {
            **{name: _jsonable(getattr(preset, name)) for name in _CATALOGUE_FIELDS},
            # Derived, not declared: `custom` is the row that ships no base URL, and
            # capacity falls out of the reference encoding.
            "needs_base_url": not preset.base_url,
            "max_references": reference_capacity(preset, ceiling),
        }
        for preset in PRESETS
    ]


def _jsonable(value: Any) -> Any:
    """Preset tuples are declared frozen; the wire wants arrays."""
    return list(value) if isinstance(value, tuple) else value


def _parse_ratio(candidate: str) -> float | None:
    left, _, right = candidate.partition(":")
    try:
        numerator, denominator = float(left), float(right)
    except ValueError:
        return None
    return numerator / denominator if denominator else None


def aspect_for(preset: ProviderPreset, width: int, height: int) -> tuple[str, str | None]:
    """The declared aspect ratio nearest to `width`x`height`, and any disclosure."""
    if not preset.aspect_ratios or width <= 0 or height <= 0:
        return "", None
    target = width / height
    best = min(preset.aspect_ratios, key=lambda candidate: ratio_distance(target, _parse_ratio(candidate)))
    chosen = _parse_ratio(best)
    if chosen is None or chosen <= 0:
        # Nothing usable on this row, so there is no ratio to send and nothing
        # truthful to say about one.
        return "", None
    if abs(chosen - target) / target <= ASPECT_NOTE_THRESHOLD:
        return best, None
    return best, f"{preset.label} renders fixed aspect ratios; {width}x{height} was rendered as {best}"


def size_for(preset: ProviderPreset, width: int, height: int) -> tuple[str, str | None]:
    """The declared `size` string nearest to `width`x`height`, and any disclosure.

    Reached only for a `size` provider, so a preset that declares no menu is taken
    to accept the request verbatim rather than being sent nothing. Ties on shape are
    broken by total pixels -- two candidates can share an aspect ratio.
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
        ratio = candidate_w / candidate_h if candidate_h else None
        return (ratio_distance(target, ratio), abs(candidate_w * candidate_h - width * height))

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


@dataclass
class BuiltRequest:
    """A body plus whatever the caller must disclose about building it."""

    body: dict[str, Any]
    notes: list[str] = field(default_factory=list)


# The two single-field modes are named after the field they emit, so the mode is
# also the key. `width_height` is the odd one out because it emits two.
_SINGLE_FIELD_MODES = {"aspect_ratio": aspect_for, "size": size_for}


def _dimension_fields(preset: ProviderPreset, width: int | None, height: int | None) -> BuiltRequest:
    if width is None or height is None:
        return BuiltRequest({})
    mapper = _SINGLE_FIELD_MODES.get(preset.dimension_mode)
    if mapper is not None:
        value, note = mapper(preset, width, height)
        return BuiltRequest({preset.dimension_mode: value} if value else {}, [note] if note else [])
    if preset.dimension_mode == "width_height":
        final_w, final_h, note = pixels_for(preset, width, height)
        return BuiltRequest({"width": final_w, "height": final_h}, [note] if note else [])
    # "none", and anything a future row misspells: the provider decides.
    return BuiltRequest({})


def _matches(model: str, markers: Sequence[str]) -> bool:
    """The one matching rule the per-model capability holes share: a declared marker
    as a lowercase substring of the model id. Never a family name inferred from
    another -- every marker is one somebody probed."""
    lowered = model.lower()
    return any(marker in lowered for marker in markers)


def drops_negative_prompt(preset: ProviderPreset, model: str) -> bool:
    """True when this model accepts `negative_prompt` and does nothing with it."""
    return _matches(model, preset.negative_prompt_blind)


def _prompt_field(preset: ProviderPreset, prompt: str) -> BuiltRequest:
    if len(prompt) <= preset.max_prompt:
        return BuiltRequest({"prompt": prompt})
    return BuiltRequest(
        {"prompt": prompt[: preset.max_prompt]},
        [f"the prompt was truncated to {preset.label}'s {preset.max_prompt}-character limit"],
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
    body: dict[str, Any] = {"model": model, **built.body}
    notes = list(built.notes)

    if n != 1:
        body["n"] = n

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
            notes.append(f"{model} ignores negative prompts, so the negative prompt had no effect")
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
    return f"data:{reference.mime};base64,{base64.b64encode(reference.data).decode('ascii')}"


# The array encodings, mapped to the key each wraps its URI in. Two providers take an
# array under the same field name and disagree about the element: xAI wants
# `{"url": ...}`, OpenAI wants `{"image_url": ...}` and rejects the other by name.
_LIST_ENCODINGS = {"url_objects": "url", "image_url_objects": "image_url"}


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
    uris = [_data_uri(reference) for reference in references]
    if not uris:
        # The caller routes a referenceless render to `build_generation_body`, so
        # this is unreachable in practice -- but returning early beats emitting the
        # reference field as a JSON `null` for a provider to make sense of.
        return built
    if preset.reference_encoding == "string_list":
        built.body[preset.reference_field] = uris
    elif preset.reference_encoding in _LIST_ENCODINGS:
        key = _LIST_ENCODINGS[preset.reference_encoding]
        built.body[preset.reference_field] = [{key: uri} for uri in uris]
    else:
        built.body[preset.reference_field] = uris[0] if preset.reference_encoding == "string" else {"url": uris[0]}
        # Only a list encoding carries more than one, so anywhere else the extras
        # are dropped -- say so rather than let the user wonder which one won.
        if len(uris) > 1:
            built.notes.append(f"{preset.label} accepts one reference image; only the first was sent")
    if preset.reference_drives_size:
        # Verified on Kontext: a 512x512 reference on a 1024x576 request came back
        # 1024x1024. Disclosed rather than left to be noticed, because the picker
        # still shows the resolution that no longer applies.
        built.notes.append("the reference image set the output size, so the resolution setting did not apply")
    return built


def takes_references(preset: ProviderPreset) -> bool:
    """True when this provider has a JSON field to put a reference in at all.

    Provider-level and nothing finer. Which *models* behind it accept one was a
    hand-kept allowlist over catalogues that grow without us, and being wrong in the
    withholding direction is invisible: the user configured a likeness, paid for the
    render, and got neither the picture nor a word about it. A model that will not
    take what it was sent refuses, the refusal is free, and `engine/degrade.py`
    re-renders without the references and says so.
    """
    return preset.supports_references


def reference_capacity(preset: ProviderPreset, ceiling: int) -> int:
    """How many references this provider's dialect can physically carry, capped.

    Derived from the encoding rather than declared, because the encoding is the only
    thing that actually constrains it: a list field takes a list, a scalar field takes
    one, and no amount of measurement changes either. Whether the model *reads* every
    element is a different question, and one this deliberately does not try to answer
    -- guessing high costs an upload, and the prompt no longer depends on the guess.

    `ceiling` is the caller's (`MAX_REFERENCE_SLOTS`), passed in so this module keeps
    knowing nothing about the picker or the config it is stored under.
    """
    if not preset.supports_references:
        return 0
    carries_list = preset.reference_encoding == "string_list" or preset.reference_encoding in _LIST_ENCODINGS
    return max(1, ceiling) if carries_list else 1
