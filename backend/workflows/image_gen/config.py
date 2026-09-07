"""Strict normalization for the image generation workflow configuration."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any, NamedTuple
from urllib.parse import SplitResult, urlsplit

from .pov import DEFAULT_MODE as DEFAULT_POV_MODE
from .pov import normalize_mode as normalize_pov_mode

WORKFLOW_ID = "image_gen"
SOURCES = ("external_comfy", "cloud")
DEFAULT_SOURCE = "external_comfy"
# The reserved connection id for the ComfyUI server -- the one connection that
# always exists and cannot be removed. Every other id in that namespace is a cloud
# provider id, so nothing may ship a provider preset called "comfy".
COMFY_CONNECTION = "comfy"
MAX_STYLES = 32
MAX_USER_GRAPHS = 32
# Switching provider must not destroy the previous key, so the credential map is
# retained rather than replaced -- and, like everything here, bounded.
MAX_CLOUD_PROVIDERS = 16
CLOUD_QUALITIES = ("low", "medium", "high")
# Canonical pixel bounds, shared by both backends. Stored as width/height even for
# providers that speak aspect ratios: one representation, converted at the wire. A
# ComfyUI graph reads the same pair, through whichever node its `width`/`height`
# slots map -- and ignores it entirely when it maps none.
MIN_CLOUD_EDGE = 64
MAX_CLOUD_EDGE = 4096
DEFAULT_CLOUD_EDGE = 1024
MAX_GRAPH_BYTES = 512_000
# The most images one render will ever carry: the ceiling on a cloud provider's
# reference array, and the most image inputs Orb tracks on an imported graph.
MAX_REFERENCE_SLOTS = 4
# The picker's 10 MB raw cap plus base64's 4/3. Bounded because the profile lives
# on `character_cards.workflow_state` and is read on every generate.
MAX_REFERENCE_IMAGE_B64 = 13_400_000
MAX_SEED = 2**64 - 1
PROMPT_FORMATS = ("tags", "hybrid", "prose")
DEFAULT_PROMPT_FORMAT = "hybrid"
# The three formats Orb carries end to end, each mapped to the extension it is named
# by. One table rather than three, because "a mime Orb accepts" and "a mime Orb can
# name as a file" are the same set: ComfyUI's multipart upload takes the extension
# from here, and so does a stored attachment's filename.
MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
REFERENCE_MIMES = tuple(MIME_EXTENSIONS)


class SourcePolicy(NamedTuple):
    """What kinds of image a style asks for, and how the kinds combine.

    `kinds` is ordered. `all_of` says whether every kind contributes (`character` then
    `previous`) or whether the first kind that resolves wins and the rest are its
    fallbacks -- which is what `previous_or_character` has always meant.
    """

    kinds: tuple[str, ...]
    all_of: bool = False


# Where a style's reference images come from. The combined `previous_or_character` is
# the default so the choice has no cold-start cliff: a style pinned to `previous` alone
# hard-fails on a new conversation's first Visualize.
#
# **One reference image per character.** `character` means the people this render is a
# picture *of*, one image each and never the same person twice -- in a solo chat that is
# one image, in a group it is one per member in frame, in `subjects.py`'s order. What a
# style no longer does is say *which* image goes in *which* slot: position is the
# subject order, so there is nothing positional left to configure and no `cast` ordinal
# to count. How many actually travel is the render target's to cap.
REFERENCE_SOURCES: dict[str, SourcePolicy] = {
    "previous": SourcePolicy(("previous",)),
    "character": SourcePolicy(("character",)),
    "previous_or_character": SourcePolicy(("previous", "character")),
    "character_and_previous": SourcePolicy(("character", "previous"), all_of=True),
}
# The names this field used to carry, and what a stored one now means. Both asked for a
# likeness of somebody in the scene, which `character` now sends for everyone in frame.
RETIRED_REFERENCE_SOURCES = {"cast": "character", "cast_or_character": "character"}
DEFAULT_REFERENCE_SOURCE = "previous_or_character"

CONFIG_DEFAULTS = {
    "source": DEFAULT_SOURCE,
    "default_style": "realistic",
    # Top level, shared by every source: a style is a way of writing the prompt, and
    # that survives a backend switch. `connection` is deliberately unlinked on both
    # shipped styles -- hard-pinning ComfyUI would make `source` a dead letter (the
    # derivation below would override it on every fresh config) and would re-pin a
    # style the user relinked if the defaults were re-seeded. The panel resolves ""
    # to whatever `source` says and writes the explicit id on first save.
    #
    # Deliberately silent about the render target -- no `model`, size or quality
    # here. These rows are parsed through `_style` like any other, so declaring a
    # size would out-rank the legacy `cloud.*` block they must inherit from: an
    # install that configured cloud before styles were stored would silently reset
    # to 1024x1024 on the first read.
    "styles": [
        {
            "id": "realistic",
            "label": "Realistic",
            "prompt_format": DEFAULT_PROMPT_FORMAT,
            "prompt": "RAW photo, realistic illumination, realistic shadows, photography, photorealistic, cinematic lighting, detailed skin, high contrast",
            "negative_prompt": "cartoon, anime, drawing, paint, flat, illustration, painting, low detail, low quality, worst quality, bad quality, bad perspective",
            "extra_instructions": "",
            "connection": "",
            "checkpoint": "",
            "workflow": "",
        },
        {
            "id": "anime",
            "label": "Anime",
            "prompt_format": DEFAULT_PROMPT_FORMAT,
            "prompt": "masterpiece, best quality, anime illustration, very aesthetic, very detailed, high contrast, good perspective",
            "negative_prompt": "photorealistic, pixelated, 3d render, muddy colors, low quality, worst quality, bad quality, score_1, score_2, bad fingers, missing fingers, fused fingers, bad anatomy, bad hair, bad perspective, bad face",
            "extra_instructions": "",
            "connection": "",
            "checkpoint": "",
            "workflow": "",
        },
    ],
    "pov_mode": DEFAULT_POV_MODE,
    "scene_analysis": False,
    "prompter_reasoning": False,
    "timeout_seconds": 180.0,
    "external_comfy": {
        "api_url": "http://127.0.0.1:8188",
        "api_key": "",
        # A ComfyUI graph is meaningless to any other backend, so this one stays put.
        "user_graphs": [],
    },
    # Connectivity only: an address and a credential, exactly as wide as
    # `external_comfy`'s. What an image looks like belongs to the style.
    "cloud": {
        "provider": "xai",
        # One entry per cloud connection, keyed by provider id. One representative
        # entry ships so the preset-schema coverage walker can see the `api_key` leaf
        # under the map level; it is inert and the panel does not list it until it
        # holds something.
        "providers": {"xai": {"api_key": "", "base_url": ""}},
    },
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _text(value: Any, limit: int, default: str = "") -> str:
    return value.strip()[:limit] if isinstance(value, str) else default


def _edge(value: Any, default: int) -> int:
    try:
        pixels = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(MAX_CLOUD_EDGE, max(MIN_CLOUD_EDGE, pixels))


def _enabled(value: Any) -> bool:
    """An opt-out setting that keeps existing configurations enabled."""
    return value if isinstance(value, bool) else True


def _seed_max(value: Any) -> str:
    """An optional inclusive ceiling, stored as decimal text for JS precision."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        ceiling = int(value)
    except (TypeError, ValueError):
        return ""
    return str(min(MAX_SEED, max(0, ceiling)))


def _source_name(value: Any) -> str:
    """One stored source name, or "" when it names nothing this build resolves."""
    name = _text(value, 32)
    name = RETIRED_REFERENCE_SOURCES.get(name, name)
    return name if name in REFERENCE_SOURCES else ""


def _first_source(values: Any) -> str:
    """The first live source in a stored *list* of them.

    A style could once point each of its target's slots somewhere of its own, and the
    entry that fed slot 0 is the one that survives: it is the slot every target has,
    and the one a solo render was always about.
    """
    if not isinstance(values, (list, tuple)):
        return ""
    return next((name for value in values for name in (_source_name(value),) if name), "")


def _reference_source(raw: Mapping[str, Any], legacy_slots: Sequence[Any], legacy_scalar: Any) -> str:
    """Where this style's reference image comes from -- one source, for every image
    input the render target declares.

    **Membership, not truthiness**, at each step: `""` is a real stored value ("send no
    reference"), so a style saved once must not inherit an older shape back on.

    Two older shapes migrate here, and the migration is the whole of it -- the positional
    list a style stored while it could answer per slot, and (for a cloud style only) the
    lone scalar the cloud block held before that. `legacy_slots` is the third: what a
    ComfyUI graph pinned per slot back when the source lived on the graph. Once no
    install predates any of them, this reduces to reading `raw`.
    """
    if "reference_source" in raw:
        return _source_name(raw["reference_source"])
    if "reference_sources" in raw:
        return _first_source(raw["reference_sources"])
    if legacy_slots:
        return _first_source(legacy_slots)
    return _source_name(legacy_scalar)


def _render_target(
    raw: Mapping[str, Any],
    cloud: Mapping[str, Any],
    connection: str,
    workflow: str,
    legacy_slots: Sequence[str],
) -> dict:
    """Resolve the fields that control one render."""
    # An unlinked style renders on `cloud.provider`, so that is the entry it inherits
    # from -- which is what makes the migration a no-op for what it next produces.
    # Guarded on a non-empty id: `""` is not a connection, and `_cloud` drops an entry
    # keyed by it, so inheriting from one would seed a style off a row that is about
    # to cease to exist.
    entries = cloud.get("providers")
    source_id = connection or _text(cloud.get("provider"), 64)
    entry = entries.get(source_id) if source_id and isinstance(entries, Mapping) else None
    sources = (raw, entry if isinstance(entry, Mapping) else {}, cloud)

    def inherited(name: str) -> Any:
        return next((s[name] for s in sources if isinstance(s, Mapping) and name in s), None)

    quality = _text(inherited("quality"), 16).lower()
    # A graph-bound style deliberately does **not** inherit the lone cloud-block
    # `reference_source`: it reaches every style through the raw cloud block whatever the
    # connection, so honouring it here would silently start uploading conversation images
    # to a ComfyUI server that never asked.
    return {
        # "" means "the provider's own default", resolved at the adapter where the
        # preset table lives -- not substituted here, so relinking to a provider with
        # a different default does not need the stored value rewritten.
        "model": _text(inherited("model"), 256),
        "width": _edge(inherited("width"), DEFAULT_CLOUD_EDGE),
        "height": _edge(inherited("height"), DEFAULT_CLOUD_EDGE),
        "quality": quality if quality in CLOUD_QUALITIES else "",
        # Compatibility is user-owned. Provider refusals are never parsed to mutate
        # these fields behind the user's back.
        "send_seed": _enabled(raw.get("send_seed")),
        "seed_max": _seed_max(raw.get("seed_max")),
        "send_negative_prompt": _enabled(raw.get("send_negative_prompt")),
        "reference_source": _reference_source(raw, legacy_slots, None if workflow else inherited("reference_source")),
    }


def _style(raw: Any, cloud: Mapping[str, Any], legacy_references: Mapping[str, Sequence[str]]) -> dict | None:
    """Resolve one image style entry."""
    if not isinstance(raw, Mapping):
        return None
    sid = _text(raw.get("id"), 64)
    if not _ID_RE.fullmatch(sid):
        return None
    connection = _text(raw.get("connection"), 64)
    if connection and not _ID_RE.fullmatch(connection):
        connection = ""
    prompt_format = _text(raw.get("prompt_format"), 16, DEFAULT_PROMPT_FORMAT).lower()
    if prompt_format not in PROMPT_FORMATS:
        prompt_format = DEFAULT_PROMPT_FORMAT
    # "external_core" was the shipped default graph and no longer exists; a config
    # still naming it must read as "assign a workflow", not as a dangling reference.
    workflow = _text(raw.get("workflow"), 64)
    if workflow == "external_core":
        workflow = ""
    return {
        "id": sid,
        "label": _text(raw.get("label"), 80, sid) or sid,
        "prompt_format": prompt_format,
        "prompt": _text(raw.get("prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
        "extra_instructions": _text(raw.get("extra_instructions"), 2_000),
        "connection": connection,
        "checkpoint": _text(raw.get("checkpoint"), 512),
        "workflow": workflow,
        **_render_target(raw, cloud, connection, workflow, legacy_references.get(workflow) or ()),
    }


def _slot(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    node, field = value
    if not isinstance(node, (str, int)) or not isinstance(field, str):
        return None
    node_s, field_s = str(node), field.strip()
    if not node_s or len(node_s) > 64 or not field_s or len(field_s) > 128:
        return None
    return [node_s, field_s]


def _declared_references(raw: Any) -> list[tuple[dict, str]]:
    """This graph's image slots, each paired with the source a legacy config pinned to it.

    The slot is **structural** -- which node inputs load an image is a fact about the
    graph, discovered at import against ComfyUI's `/object_info` -- so it is stored
    here, for every image widget the importer found rather than only the ones pointed
    somewhere. Where the bytes come from is not a fact about the graph: that is the
    style's to say, so two styles on one workflow can differ and either can send no
    reference at all. The second element is only what an older config recorded, for
    `_style` to migrate onto the styles using this graph.
    """
    entries: list[tuple[dict, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        slot = _slot(item.get("slot"))
        if slot is None:
            continue
        # One entry per widget: two rows on one slot would both resolve and both be
        # recorded, but only the second survives patching, so the record would claim
        # a reference the render never used.
        if (slot[0], slot[1]) in seen:
            continue
        seen.add((slot[0], slot[1]))
        entries.append(
            (
                {"slot": slot, "label": _text(item.get("label"), 120) or f"{slot[0]} — {slot[1]}"},
                _source_name(item.get("source")),
            )
        )
        if len(entries) >= MAX_REFERENCE_SLOTS:
            break
    return entries


def _strip_machine_local_state(graph: dict) -> dict:
    """A deep copy of `graph` with each node's top-level `is_changed` removed.

    ComfyUI's API export embeds `is_changed` -- for a `LoadImage`, a hash of the file
    on the *exporter's* disk. `IsChangedCache.get` returns the client-supplied value
    verbatim as part of the node's cache signature, so a pinned hash masks a change
    of file contents at an unchanged path and the render returns the previously
    decoded image. Stripped before the size cap is measured.
    """
    stripped = copy.deepcopy(graph)
    for node in stripped.values():
        if isinstance(node, dict):
            node.pop("is_changed", None)
    return stripped


def _user_graph(raw: Any, legacy_sources: dict[str, list[str]] | None = None) -> dict | None:
    """One imported graph, optionally recording what an older config pinned per slot.

    `legacy_sources` is filled in only for a graph that actually parses, and only for
    the first entry claiming an id -- exactly the ones `_unique_by_id` keeps, so the
    style inheriting from a graph id never inherits from a row that was discarded.
    """
    if not isinstance(raw, Mapping):
        return None
    gid = _text(raw.get("id"), 64)
    graph = raw.get("graph")
    slots_raw = raw.get("slots")
    if not _ID_RE.fullmatch(gid) or not isinstance(graph, dict) or not isinstance(slots_raw, Mapping):
        return None
    import json

    graph = _strip_machine_local_state(graph)
    if len(json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_GRAPH_BYTES:
        return None
    slots: dict[str, Any] = {}
    for name in ("positive", "negative", "seed", "output", "checkpoint", "width", "height"):
        parsed = _slot(slots_raw.get(name))
        if parsed is not None:
            slots[name] = parsed
    # `negative`, `checkpoint` and the two size slots stay optional: a one-encoder
    # prose graph has nothing to map negative to, a self-contained graph keeps its own
    # model, and a graph whose output size comes from a reference image or an
    # aspect-ratio node has no width/height pair to patch. Absent is how "unmapped"
    # is encoded, which is also why no migration is needed for either.
    if not all(name in slots for name in ("positive", "seed", "output")):
        return None
    # Also optional, so a t2i graph normalizes unchanged: a graph that loads no image
    # has no `references` key at all, which is how "not an edit workflow" is encoded.
    # "Off" is not encoded here any more -- it is a style saying `""` for this slot.
    declared = _declared_references(slots_raw.get("references"))
    references = [entry for entry, _ in declared]
    if references:
        slots["references"] = references
    if legacy_sources is not None:
        legacy_sources.setdefault(gid, [source for _, source in declared])
    return {
        "id": gid,
        "label": _text(raw.get("label"), 100, gid) or gid,
        "graph": graph,
        "slots": slots,
    }


def _is_loopback(hostname: str) -> bool:
    host = hostname.lower()
    return host in ("localhost", "::1", "0:0:0:0:0:0:0:1") or host == "127.0.0.1" or host.startswith("127.")


def _is_addressable(parsed: SplitResult) -> bool:
    """Whether Orb will talk to this URL at all -- the rule both endpoint fields
    share. Embedded credentials are refused rather than carried: they would ride
    every request URL into logs and back out to the settings form."""
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password


def _cloud_base_url(value: Any) -> str:
    """A user-supplied cloud endpoint override, or "" to use the preset's own.

    The ComfyUI URL's rules plus one more: a cloud request carries a bearer key on
    every call, so plaintext is tolerable only when the "network" is this machine.
    """
    url = _text(value, 2_048)
    if not url:
        return ""
    parsed = urlsplit(url)
    if not _is_addressable(parsed):
        return ""
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname or ""):
        return ""
    return url.rstrip("/")


def _cloud_provider_entry(raw: Any) -> dict:
    """One cloud connection: an address and a credential, and nothing else.

    Exactly as wide as the ComfyUI connection's `{api_url, api_key}`, because a
    connection is how Orb *reaches* a backend. What an image looks like -- the model,
    the resolution, the quality, whether a reference rides along -- is what a style
    is, and lives there, so two styles on one provider can differ.

    Those four did live here. They are not read off `raw` any more because `_style`
    reads them off the same raw block on the way past; dropping them here is what
    completes the migration on the next write.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "api_key": _text(raw.get("api_key"), 2_048),
        "base_url": _cloud_base_url(raw.get("base_url")),
    }


def _cloud(raw: Any, provider_override: str = "") -> dict:
    """Resolve cloud image provider settings."""
    raw = raw if isinstance(raw, Mapping) else {}
    defaults = CONFIG_DEFAULTS["cloud"]
    provider = _text(provider_override or raw.get("provider"), 64, defaults["provider"])
    if not _ID_RE.fullmatch(provider):
        provider = defaults["provider"]

    raw_map = raw.get("providers")
    raw_map = raw_map if isinstance(raw_map, Mapping) else {}
    providers: dict[str, dict] = {}
    # The selected provider goes in first, so the cap can never be the reason the
    # credentials the user is actively using are the ones that go missing.
    ordered = [(provider, raw_map.get(provider))] + [(key, value) for key, value in raw_map.items() if key != provider]
    for candidate, entry in ordered:
        pid = _text(candidate, 64)
        if not _ID_RE.fullmatch(pid) or pid in providers:
            continue
        providers[pid] = _cloud_provider_entry(entry)
        if len(providers) >= MAX_CLOUD_PROVIDERS:
            break

    return {"provider": provider, "providers": providers}


def _unique_by_id(candidates: Any, parse: Any, limit: int) -> list[dict]:
    """Parse each candidate, dropping what does not normalize or repeats an id."""
    items: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates if isinstance(candidates, list) else []:
        item = parse(candidate)
        if item and item["id"] not in seen:
            items.append(item)
            seen.add(item["id"])
        if len(items) >= limit:
            break
    return items


def normalize_config(raw: Mapping[str, Any] | None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    external_value = raw.get("external_comfy")
    external_raw: Mapping[str, Any] = external_value if isinstance(external_value, Mapping) else {}
    url = _text(external_raw.get("api_url"), 2_048, CONFIG_DEFAULTS["external_comfy"]["api_url"])
    if not _is_addressable(urlsplit(url)):
        url = CONFIG_DEFAULTS["external_comfy"]["api_url"]
    url = url.rstrip("/")

    # Styles were nested inside `external_comfy` before they were shared. The
    # normalizer runs on every read and every write, so a legacy config hoists on
    # first read and persists hoisted on first write -- no DB migration.
    raw_styles = raw.get("styles")
    if not isinstance(raw_styles, list):
        raw_styles = external_raw.get("styles")
    if not isinstance(raw_styles, list):
        raw_styles = CONFIG_DEFAULTS["styles"]
    cloud_value = raw.get("cloud")
    cloud_raw: Mapping[str, Any] = cloud_value if isinstance(cloud_value, Mapping) else {}
    # Graphs parse first because a style inherits from the one it names: the sources
    # now on the style were stored per slot on the graph, and `_user_graph` drops them
    # on the way past, so it hands them out here on its way past instead.
    legacy_references: dict[str, list[str]] = {}
    graphs = _unique_by_id(
        external_raw.get("user_graphs"), partial(_user_graph, legacy_sources=legacy_references), MAX_USER_GRAPHS
    )
    # The raw cloud block and the legacy map are bound at the call site rather than by
    # widening `_unique_by_id`, which `_user_graph` shares and has neither to pass.
    parse_style = partial(_style, cloud=cloud_raw, legacy_references=legacy_references)
    # The shipped defaults go through the same parse rather than being copied in
    # whole, so every path out of here produces one shape -- and so a config that
    # never stored a style still inherits the cloud block it was configured with.
    styles = _unique_by_id(raw_styles, parse_style, MAX_STYLES) or _unique_by_id(
        CONFIG_DEFAULTS["styles"], parse_style, MAX_STYLES
    )

    default_style = _text(raw.get("default_style"), 64, styles[0]["id"])
    if default_style not in {s["id"] for s in styles}:
        default_style = styles[0]["id"]
    try:
        timeout = float(raw.get("timeout_seconds", 180.0))
    except (TypeError, ValueError):
        timeout = 180.0

    source = _text(raw.get("source"), 32, DEFAULT_SOURCE)
    if source not in SOURCES:
        source = DEFAULT_SOURCE
    # `source` is derived, not chosen: the form has a connection per style, so which
    # backend routes is a property of the style, not of the config. Kept here only so
    # `_status` and a stored attachment's record still have a global answer -- the
    # render path routes per style through `style_source`, which is this same call
    # with whichever style is actually about to render.
    source, provider_override = style_source(
        {"source": source, "cloud": cloud_raw},
        active_style({"styles": styles, "default_style": default_style}),
    )

    return {
        "source": source,
        "default_style": default_style,
        "styles": styles,
        "pov_mode": normalize_pov_mode(raw.get("pov_mode")),
        "scene_analysis": bool(raw.get("scene_analysis", False)),
        "prompter_reasoning": raw.get("prompter_reasoning") is True,
        "timeout_seconds": min(900.0, max(10.0, timeout)),
        "external_comfy": {
            "api_url": url,
            "api_key": _text(external_raw.get("api_key"), 2_048),
            "user_graphs": graphs,
        },
        "cloud": _cloud(raw.get("cloud"), provider_override),
    }


def style_source(config: Mapping[str, Any], style: Mapping[str, Any]) -> tuple[str, str]:
    """Return the provider source for one style."""
    connection = _text(style.get("connection"), 64) if isinstance(style, Mapping) else ""
    if connection == COMFY_CONNECTION:
        return "external_comfy", ""
    if connection:
        return "cloud", connection
    cloud = config.get("cloud")
    source = _text(config.get("source"), 32, DEFAULT_SOURCE) or DEFAULT_SOURCE
    provider = _text(cloud.get("provider"), 64) if isinstance(cloud, Mapping) else ""
    return source, (provider if source == "cloud" else "")


def active_style(config: Mapping[str, Any]) -> dict:
    """The style the next render will use, unless a trigger names another.

    The one place "which style is in play" is decided when nothing names one: the
    tools-panel card's status line, and any adapter built without a bound style.
    """
    styles = config["styles"]
    return next((s for s in styles if s["id"] == config["default_style"]), styles[0])


def resolve_style(config: Mapping[str, Any], style_id: str) -> dict:
    style = next((s for s in config["styles"] if s["id"] == style_id), None)
    if style is None:
        raise ValueError(f"unknown image style {style_id!r}")
    # An empty workflow stays empty: external mode has no default graph, so the
    # render path turns "no workflow" into an "assign one" error rather than
    # silently substituting.
    return dict(style)


def style_reference_source(style: Mapping[str, Any]) -> str:
    """Where this style draws its one reference image from, or "" for none.

    A bare read rather than `normalize_config`'s guarantee, because `validate_connection`
    walks every style in the config and one hand-edited row must not turn Test connection
    into a 500. Shared by both adapters so neither invents its own read of the field.
    """
    return _text(style.get("reference_source"), 32)


def normalize_profile(raw: Mapping[str, Any] | None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    # The per-character reference, for slots resolving to `character`. Dropped rather
    # than truncated when oversized (half a base64 payload is not a smaller image),
    # and both halves ride together: bytes with no mime cannot be read.
    image_raw = raw.get("reference_image_b64")
    image = image_raw.strip() if isinstance(image_raw, str) else ""
    mime = _text(raw.get("reference_mime"), 64).lower()
    if len(image) > MAX_REFERENCE_IMAGE_B64 or mime not in REFERENCE_MIMES:
        image, mime = "", ""
    if not image:
        mime = ""
    return {
        "appearance_prompt": _text(raw.get("appearance_prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
        "reference_image_b64": image,
        "reference_mime": mime,
    }
