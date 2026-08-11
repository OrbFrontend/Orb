"""
lorebook.py — Lorebook activation: one pipeline, three sources.

A lorebook entry activates from any of three sources:
  * ``constant`` — always active; rides the cached system-prompt prefix
    (:func:`compute_constant_lorebook_block`), never the trailing block. Unless
    it also sets ``at_depth``, in which case it rides the per-turn tail instead
    (:func:`compute_depth_lorebook_block`) — the ``@ Depth`` placement.
  * keyword scan — a keyword appears (substring) within the last ``scan_depth`` messages.
  * director pick — the agentic Director named the entry.

The trailing block carries only the per-turn sources: substring mode uses
{keyword@6}; agentic mode adds the director-pick source and scans shallower
({keyword@2, director-pick}) since the Director already saw the history. Both
modes funnel through :func:`select_active_entries` → :func:`render_lorebook_block`,
so the two named entry points below (:func:`compute_lorebook_injection_block`,
:func:`compute_agentic_lorebook_block`) are thin wrappers over one core.

**Two layers, one effective view.** A World's rows are the user's *authored*
lore plus the Agent-managed *dynamic* overlay (see
``database/queries/worlds.py``). Nothing downstream of this module should ever
see the raw pool: :func:`select_effective_entries` resolves it — an active
replacement or suppression hides its authored target, a suppression itself
injects nothing, archived rows are gone. Every activation and rendering entry
point here applies it *for itself* (:func:`select_active_entries`,
:func:`build_lorebook_catalog`, the two constant builders), so a caller cannot
forget to project and no two callers can disagree — which is also what keeps the
pipeline's prefix bytes and the workflow toolkit's off-turn prefix identical.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import Macros

LOREBOOK_SCAN_DEPTH = 6
# The agentic fallback scan only looks at the current turn (previous assistant
# message + current user message), since the Director already saw the history.
AGENTIC_LOREBOOK_SCAN_DEPTH = 2

# The heading Agent-managed lore renders under, in every block. Kept distinct
# from the authored heading so the model (and the user reading a prompt dump)
# can always tell established lore from state the roleplay itself produced.
DYNAMIC_SECTION_TITLE = "Dynamic World State"


# ── Layer projection ──────────────────────────────────────────────────────────


def is_dynamic(entry: Mapping[str, Any]) -> bool:
    """True when *entry* is an Agent-managed overlay row rather than authored lore."""
    return entry.get("entry_layer") == "dynamic"


def select_effective_entries(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Resolve the authored + dynamic pool into the lore actually in effect.

    Three rules, applied in order:

    1. A disabled or archived row is inert. In particular, disabling an overlay
       re-exposes the authored entry it was hiding.
    2. An authored entry named by a live ``replace`` or ``suppress`` is hidden.
    3. A ``suppress`` marker injects nothing; it exists only to hide its target.

    Returns entries in input order (authored and dynamic interleaved as they
    came); ordering into sections is :func:`render_lorebook_block`'s job. Rows
    without the overlay columns — a hand-built dict in a test, a pre-migration
    row — read as authored, so this is a no-op on a World that has never used
    the feature.
    """
    live = [e for e in entries if bool(e.get("enabled", 1)) and not e.get("archived")]
    hidden: set[int] = set()
    for e in live:
        if is_dynamic(e) and e.get("overlay_action") in ("replace", "suppress"):
            target = e.get("supersedes_entry_id")
            if target is not None:
                hidden.add(int(target))
    return [e for e in live if not (is_dynamic(e) and e.get("overlay_action") == "suppress") and e.get("id") not in hidden]


# ── Activation gating ─────────────────────────────────────────────────────────


def agentic_lorebook_active(
    settings: Mapping[str, Any],
    lorebook_entries: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return True when the director should pick lorebook entries this turn.

    Requires the feature flag, the global agent on, and at least one non-constant
    entry. It is independent of ``direct_scene``: the picks run in their own
    forced ``select_lorebook`` call, so agentic lorebook works whether or not the
    Director's scene-direction tool is enabled. Constant entries are always
    injected and never managed by the director, so a pool of only constants does
    not enable agentic mode.

    *agent_on* is passed in (rather than recomputed) so ``agent_enabled`` stays
    the single source of truth — mirroring ``resolve_length_guard``.
    """
    if not bool(settings.get("agentic_lorebook_enabled", 0)):
        return False
    if not agent_on:
        return False
    return any(not e.get("constant") for e in lorebook_entries)


# ── Director-facing catalog (agentic mode) ────────────────────────────────────


def build_lorebook_catalog(entries: Sequence[Mapping[str, Any]]) -> str:
    """Build the Director's lorebook catalog for the agentic activation path.

    Lists each non-``constant`` entry (name + up to 3 keywords, excluding any
    keyword equal to the entry name), grouped by
    world. Constant entries are always injected and excluded here. Returns
    ``""`` when there are no non-constant candidates.

    Projected first, so the Director is offered the lore that is actually in
    effect: a replaced authored entry never appears alongside the dynamic entry
    that supersedes it, and a suppressed one is not offered at all.
    """
    candidates = [e for e in select_effective_entries(entries) if not e.get("constant")]
    if not candidates:
        return ""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for e in candidates:
        groups.setdefault(e.get("world_name") or "", []).append(e)

    parts = [
        "**Available Lorebook Entries** — activate the ones relevant to the scene via `selected_lorebook_entries`. Possible values are wrapped in square brackets."
    ]
    for world, items in groups.items():
        if world:
            parts.append(f"### {world}")
        for e in items:
            name = e.get("name", "")
            name_fold = name.casefold()
            keywords = [kw for kw in (e.get("keywords", []) or []) if kw.casefold() != name_fold]
            kws = ", ".join(keywords[:3])
            parts.append(f"- [{name}] — {kws}" if kws else f"- [{name}]")
    return "\n".join(parts)


# ── Activation sources + selection ────────────────────────────────────────────


def _any_keyword_hit(
    keywords: Sequence[str],
    scan_text: str,
    lowered: str,
    *,
    use_regex: bool,
    case_insensitive: bool,
) -> bool:
    """True when any of *keywords* hits *scan_text* (substring, or regex under V3's ``use_regex``)."""
    flags = re.IGNORECASE if case_insensitive else 0
    for kw in keywords:
        if use_regex:
            # `re` caches compiled patterns internally — no manual cache
            # for a ~55-entry scan. A literal key that happens to be valid regex
            # (`C++`, `(H)`) is *reinterpreted*, which is what a spec-compliant
            # reader does; the except only nets syntactically invalid patterns
            # (`*star`), which degrade to the substring path below.
            try:
                if re.search(kw, scan_text, flags):
                    return True
                continue
            except re.error:
                pass
        if (kw.lower() if case_insensitive else kw) in (lowered if case_insensitive else scan_text):
            return True
    return False


def select_keyword_entries(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    scan_depth: int = LOREBOOK_SCAN_DEPTH,
) -> list[Mapping[str, Any]]:
    """Select lorebook entries by keyword scan.

    A pure keyword source: entries are selected when any keyword appears
    (substring match, or a regex match when the entry sets ``use_regex``) in the
    ``scan_depth`` most recent messages. A ``selective`` entry additionally
    requires one of its ``secondary_keys`` to hit. Constant entries are not this
    source's concern — they ride the system prefix. Returns matched entries in
    input order.
    """
    scan_parts = [m.get("content") or "" for m in messages[-scan_depth:] if m.get("content")]
    scan_text = " ".join(scan_parts)
    lowered = scan_text.lower()
    matched: list[Mapping[str, Any]] = []

    for entry in entries:
        keywords = entry.get("keywords", [])
        if not keywords or not scan_text:
            continue

        rx = bool(entry.get("use_regex"))
        ci = bool(entry.get("case_insensitive", True))
        if not _any_keyword_hit(keywords, scan_text, lowered, use_regex=rx, case_insensitive=ci):
            continue

        # The importer only sets `selective` when secondary_keys is non-empty
        # (blanket-selective cards would otherwise match nothing), so the
        # emptiness check here is belt-and-braces for hand-edited rows.
        secondary = entry.get("secondary_keys") or []
        if (
            entry.get("selective")
            and secondary
            and not _any_keyword_hit(secondary, scan_text, lowered, use_regex=rx, case_insensitive=ci)
        ):
            continue

        matched.append(entry)

    return matched


def select_active_entries(
    entries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    scan_depth: int,
    director_selected: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    """Select the active lorebook entries for the trailing block.

    An entry is active when a keyword matched within the ``scan_depth`` most
    recent messages, OR its ``name`` is in *director_selected* (case-insensitive,
    trimmed). The pool is projected to the effective layer first, then constant
    entries are excluded — they ride the cached system prefix, and filtering
    here (rather than per caller) also keeps a director pick that names a
    constant entry from duplicating it into the trailing block. Returns entries
    in input order — the union underlying both the substring
    (``director_selected=()``) and agentic paths.
    """
    entries = [e for e in select_effective_entries(entries) if not e.get("constant")]
    director_named = {(n or "").strip().casefold() for n in director_selected}
    keyword_hit = {id(e) for e in select_keyword_entries(messages or [], entries, scan_depth)}

    def is_active(entry: Mapping[str, Any]) -> bool:
        name = (entry.get("name", "") or "").strip().casefold()
        return id(entry) in keyword_hit or name in director_named

    return [e for e in entries if is_active(e)]


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render_section(entries: Sequence[Mapping[str, Any]], resolve, header: str) -> list[str]:
    """Render one layer's entries under *header*, or nothing when it is empty."""
    if not entries:
        return []
    matched = sorted(entries, key=lambda e: (-e.get("priority", 100), e.get("sort_order", 0), e.get("id", 0)))
    parts = [header]
    for entry in matched:
        name = resolve(entry.get("name", ""))
        content = resolve(entry.get("content", ""))
        if name and content:
            parts.append(f"{name}: {content}")
        elif content:
            parts.append(content)
    # A section whose every entry rendered blank contributes no header either.
    return parts if len(parts) > 1 else []


def render_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
    *,
    header: str = "**Lorebook**",
    dynamic_header: str = f"**{DYNAMIC_SECTION_TITLE}**",
) -> str:
    """Render already-selected lorebook entries into a *header*-titled block.

    The single rendering point for every activation path. Authored entries come
    first under *header*; Agent-managed ones follow under *dynamic_header*, so
    the model reads established lore before the state the roleplay has since
    produced. Within each section entries are sorted by priority DESC, then
    sort_order ASC, id ASC (the canonical lorebook order, so the block bytes are
    stable across turns regardless of input order — KV cache); names and content
    are macro-resolved. Returns ``""`` when *entries* is empty.
    """
    if not entries:
        return ""

    resolve = macros.resolve_message if macros else (lambda t: t)
    parts = _render_section([e for e in entries if not is_dynamic(e)], resolve, header)
    parts += _render_section([e for e in entries if is_dynamic(e)], resolve, dynamic_header)
    return "\n\n".join(parts)


# ── Block builders ────────────────────────────────────────────────────────────


def compute_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    scan_depth: int,
    director_selected: Sequence[str] = (),
    macros: Macros | None = None,
) -> str:
    """Select active entries (all sources) and render the ``**Lorebook**`` block.

    The shared core behind both named entry points and the pipeline's
    ``LorebookTurn.writer_block`` (``pipeline/state.py``).
    """
    return render_lorebook_block(
        select_active_entries(entries, messages, scan_depth=scan_depth, director_selected=director_selected),
        macros,
    )


def compute_lorebook_injection_block(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Substring path: build the trailing lorebook block by keyword scanning.

    Entries are included when a keyword matches within the 6 most recent
    messages. Constant entries are excluded — they ride the cached system prefix
    (:func:`compute_constant_lorebook_block`) or, with ``at_depth``, the depth
    block (:func:`compute_depth_lorebook_block`). Sorted by priority DESC.
    Returns ``""`` when nothing matches.
    """
    return compute_lorebook_block(entries, messages, scan_depth=LOREBOOK_SCAN_DEPTH, macros=macros)


def compute_agentic_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    selected_names: Sequence[str],
    macros: Macros | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Agentic path: build the trailing lorebook block from the Director's selection.

    Includes entries whose ``name`` matches *selected_names* (case-insensitive,
    trimmed) + entries triggered by a keyword scan over the current turn
    (``AGENTIC_LOREBOOK_SCAN_DEPTH``), so keywords the Director overlooks still
    activate their entries. Constant entries are excluded — they ride the cached
    system prefix (:func:`compute_constant_lorebook_block`) or, with
    ``at_depth``, the depth block (:func:`compute_depth_lorebook_block`).
    Returns ``""`` when nothing matches.
    """
    return compute_lorebook_block(
        entries,
        messages or [],
        scan_depth=AGENTIC_LOREBOOK_SCAN_DEPTH,
        director_selected=selected_names or (),
        macros=macros,
    )


def compute_constant_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Prefix path: render the ``constant`` entries as a system-prompt section.

    Constant entries are byte-identical every turn, so they live in the cached
    system-prompt prefix (``## Lorebook``, matching the prefix's ``##`` section
    register) instead of the per-turn trailing block. The canonical sort in
    :func:`render_lorebook_block` keeps the bytes stable across turns (KV cache);
    like other prefix fields, entry text should avoid ``{{roll}}`` — it re-rolls
    per resolution and would silently change prefix bytes (that is what
    ``at_depth`` is for; see :func:`compute_depth_lorebook_block`). Returns
    ``""`` when there are no prefix-bound constant entries.

    Projects the overlay first, so a dynamic entry the user accepted as
    ``constant`` joins the prefix under its own ``## Dynamic World State``
    section. Both consumers of this function — the pipeline's prefix builder and
    the workflow toolkit's off-turn one — call it with the same raw pool, so
    their constant-lore bytes stay identical by construction.
    """
    prefix_bound = [e for e in select_effective_entries(entries) if e.get("constant") and not e.get("at_depth")]
    return render_lorebook_block(prefix_bound, macros, header="## Lorebook", dynamic_header=f"## {DYNAMIC_SECTION_TITLE}")


def compute_depth_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Depth path: render the ``constant`` + ``at_depth`` entries for the tail.

    The ``@ Depth`` placement (``position: 4`` in a World Info file): always
    injected, but into the per-turn tail after the user message rather than
    the cached prefix. Two
    things follow from that placement, and both are the point of it —
    instructions land next to the generation boundary, and inline macros are
    resolved *unseeded*, so ``{{roll}}`` yields fresh dice every turn instead of
    freezing for the conversation. Unseeding happens here rather than at the
    call site so no caller can accidentally hand this block a frozen RNG.

    Costs no KV cache: the tail is rebuilt every turn regardless. Returns ``""``
    when no constant entry is depth-bound.
    """
    depth_bound = [e for e in select_effective_entries(entries) if e.get("constant") and e.get("at_depth")]
    fresh = macros._replace(seed="") if macros else None
    return render_lorebook_block(
        depth_bound, fresh, header="**Lorebook (Depth)**", dynamic_header=f"**{DYNAMIC_SECTION_TITLE} (Depth)**"
    )
