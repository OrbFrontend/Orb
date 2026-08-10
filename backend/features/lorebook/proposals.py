"""Dynamic Worlds: the Agent-facing catalog, and strict validation of what it proposes.

Pure logic. The model never executes database CRUD — it returns a
``propose_world_changes`` call, :func:`validate_proposal` turns that into a list
of normalised operations the applier can execute (or rejects it), and only then
does anything reach the database. Everything an operation is allowed to say is
checked here against the *live* World, so a malformed or hostile call is a
no-op rather than a corrupt overlay.

The catalog (:func:`build_world_change_catalog`) is the other half of the
contract: it is what makes ``target_entry_id`` meaningful. Ids are the stable
``lorebook_entries`` row ids — no parallel UUID scheme — so a proposal points at
exactly the row the drawer edits.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...inference.lorebook import (
    DYNAMIC_SECTION_TITLE,
    is_dynamic,
    select_effective_entries,
    select_keyword_entries,
)

OPERATIONS = ("create", "replace", "suppress", "update", "archive")
ACTIVATIONS = ("constant", "keywords")
EVIDENCE_SOURCES = ("user", "reply")

# The two operations that revise an existing overlay row; every other
# operation with a target names an authored one.
_TARGETS_DYNAMIC = ("update", "archive")

# How much of an entry's body the compact catalog line shows before eliding.
_COMPACT_CONTENT_CHARS = 90


@dataclass(slots=True)
class ValidatedProposal:
    """The result of vetting one ``propose_world_changes`` call.

    ``operations`` is what may be applied. ``rejected`` holds ``(index, reason)``
    for every operation dropped — logged, never silently swallowed, so a model
    that keeps proposing something invalid is diagnosable. An empty
    ``operations`` list means "no proposal", which is a normal, common outcome.
    """

    summary: str = ""
    operations: list[dict] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.operations


# ── Catalog ───────────────────────────────────────────────────────────────────


def _compact(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _COMPACT_CONTENT_CHARS else flat[: _COMPACT_CONTENT_CHARS - 1] + "…"


def _entry_line(entry: Mapping[str, Any], *, full: bool) -> str:
    """One catalog line: id, name, activation, and body (full or elided)."""
    bits = []
    if entry.get("constant"):
        bits.append("constant")
    else:
        kws = [k for k in (entry.get("keywords") or []) if k]
        bits.append("keywords: " + ", ".join(kws[:5]) if kws else "no keywords")
    action = entry.get("overlay_action")
    if action == "replace" and entry.get("supersedes_entry_id") is not None:
        bits.append(f"replaces [{entry['supersedes_entry_id']}]")
    head = f"- [{entry.get('id')}] {entry.get('name', '')} ({'; '.join(bits)})"
    body = (entry.get("content") or "").strip()
    if not body:
        return head
    return f"{head}\n  {body}" if full else f"{head}\n  {_compact(body)}"


def build_world_change_catalog(
    entries: Sequence[Mapping[str, Any]],
    *,
    exchange_text: str = "",
) -> str:
    """Render the World for the proposal step: every entry, ids attached.

    Dynamic entries always carry their full content — they are the rows the step
    may revise, so it must see exactly what they currently say. Authored entries
    are listed compactly unless they are *relevant to this exchange* (constant,
    or a keyword hit in the user message + reply), which keeps a large authored
    book from crowding out the exchange itself while still giving the step the
    full text of anything it might contradict.

    Suppressed authored entries and archived overlay rows are absent: the step
    reasons about the World as it currently reads, and re-proposing against lore
    that is not in effect is exactly the confusion the projection exists to
    prevent. Returns ``""`` for an empty World.
    """
    effective = select_effective_entries(entries)
    if not effective:
        return ""

    authored = [e for e in effective if not is_dynamic(e)]
    dynamic = [e for e in effective if is_dynamic(e)]

    relevant_ids: set[int] = {e["id"] for e in authored if e.get("constant") and e.get("id") is not None}
    if exchange_text:
        scan = [{"role": "user", "content": exchange_text}]
        for e in select_keyword_entries(scan, [e for e in authored if not e.get("constant")], scan_depth=1):
            if e.get("id") is not None:
                relevant_ids.add(int(e["id"]))

    parts: list[str] = []
    if authored:
        parts.append("### Authored")
        parts.extend(_entry_line(e, full=e.get("id") in relevant_ids) for e in authored)
    if dynamic:
        parts.append(f"### {DYNAMIC_SECTION_TITLE}")
        parts.extend(_entry_line(e, full=True) for e in dynamic)
    return "\n".join(parts)


# ── Validation ────────────────────────────────────────────────────────────────


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        kw = _clean_str(item)
        if kw and kw.casefold() not in {s.casefold() for s in seen}:
            seen.append(kw)
    return seen


def _target_id(raw: Mapping[str, Any]) -> int | None:
    value = raw.get("target_entry_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def validate_proposal(
    arguments: Mapping[str, Any] | None,
    entries: Sequence[Mapping[str, Any]],
) -> ValidatedProposal:
    """Vet a raw ``propose_world_changes`` argument dict against the live World.

    Every operation must survive all of:

    * a known ``op``;
    * a ``target_entry_id`` naming a row of the right layer, in this World, and
      currently in effect — ``replace``/``suppress`` target an *authored* entry,
      ``update``/``archive` target a *dynamic* one (the Agent may never modify or
      delete an authored row, so there is no operation that could);
    * one target per operation, and one operation per target — two operations
      aimed at the same entry are ambiguous, so the later one is dropped rather
      than guessed at;
    * non-empty name and content for anything that creates or rewrites an entry;
    * at least one keyword under ``keywords`` activation, since an entry that can
      never trigger is not lore, it is dead weight in the table;
    * a name that does not collide (case-insensitively) with another live dynamic
      entry in this World, or with an earlier create in the same proposal.

    Anything else is dropped with a reason. A proposal whose operations are all
    dropped is simply an empty proposal.
    """
    result = ValidatedProposal()
    if not isinstance(arguments, Mapping):
        return result
    result.summary = _clean_str(arguments.get("summary"))

    raw_ops = arguments.get("operations")
    if not isinstance(raw_ops, list):
        return result

    effective = select_effective_entries(entries)
    # Two lookups, because the two families of target mean different things.
    # `replace`/`suppress` act on lore that is *currently in effect*, so an
    # already-hidden authored entry is not a legal target. `update`/`archive`
    # act on an overlay row, including a suppression marker — retiring one is
    # how the Agent brings a suppressed authored entry back — so those resolve
    # against every live row instead.
    by_id = {int(e["id"]): e for e in effective if e.get("id") is not None}
    live_by_id = {int(e["id"]): e for e in entries if e.get("id") is not None and not e.get("archived")}
    # Names that would collide. Only *live* dynamic entries count: an authored
    # entry may legitimately share a name with the dynamic row replacing it.
    taken_names = {_clean_str(e.get("name")).casefold() for e in effective if is_dynamic(e) and _clean_str(e.get("name"))}
    claimed_targets: set[int] = set()

    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, Mapping):
            result.rejected.append((index, "operation is not an object"))
            continue
        op = _clean_str(raw.get("op")).lower()
        if op not in OPERATIONS:
            result.rejected.append((index, f"unknown op {op!r}"))
            continue

        target = _target_id(raw)
        entry: Mapping[str, Any] | None = None
        if op == "create":
            if target is not None:
                result.rejected.append((index, "create must not name a target entry"))
                continue
        else:
            if target is None:
                result.rejected.append((index, f"{op} needs a target_entry_id"))
                continue
            wants_dynamic = op in _TARGETS_DYNAMIC
            entry = (live_by_id if wants_dynamic else by_id).get(target)
            if entry is None:
                scope = "live overlay" if wants_dynamic else "effective lore"
                result.rejected.append((index, f"target entry {target} is not in this world's {scope}"))
                continue
            if is_dynamic(entry) is not wants_dynamic:
                layer = "dynamic" if wants_dynamic else "authored"
                result.rejected.append((index, f"{op} must target a {layer} entry; {target} is not"))
                continue
            if target in claimed_targets:
                result.rejected.append((index, f"entry {target} is already targeted by an earlier operation"))
                continue

        item: dict[str, Any] = {
            "op": op,
            "rationale": _clean_str(raw.get("rationale")),
            "evidence": (
                _clean_str(raw.get("evidence")).lower()
                if _clean_str(raw.get("evidence")).lower() in EVIDENCE_SOURCES
                else "reply"
            ),
        }
        if target is not None and entry is not None:
            item["target_entry_id"] = target
            # Snapshot what the target says *now*, so the review card can show a
            # before/after without a second query — and so applied history still
            # reads correctly once the live row has moved on. A proposal whose
            # World changed underneath it goes stale before it can be applied, so
            # the snapshot can never silently misrepresent what will happen.
            item["target_name"] = _clean_str(entry.get("name"))
            item["target_content"] = _clean_str(entry.get("content"))

        # `suppress` and `archive` carry no body: one hides an authored entry and
        # injects nothing, the other retires an overlay row it does not rewrite.
        if op in ("suppress", "archive"):
            if op == "suppress" and target is not None:
                # The marker inherits its target's name so the drawer and the
                # review card can say *what* was suppressed without a join.
                item["name"] = _clean_str(raw.get("name")) or _clean_str(by_id[target].get("name"))
            result.operations.append(item)
            if target is not None:
                claimed_targets.add(target)
            continue

        name = _clean_str(raw.get("name"))
        content = _clean_str(raw.get("content"))
        if op == "update":
            # An update may revise either field; whatever it omits keeps its
            # current value, so only a wholly empty update is meaningless.
            existing = live_by_id[int(item["target_entry_id"])]
            if not name and not content and "activation" not in raw and "keywords" not in raw:
                result.rejected.append((index, "update changes nothing"))
                continue
            name = name or _clean_str(existing.get("name"))
            content = content or _clean_str(existing.get("content"))
        if not name or not content:
            result.rejected.append((index, f"{op} needs both a name and content"))
            continue

        activation = _clean_str(raw.get("activation")).lower()
        if activation not in ACTIVATIONS:
            activation = "keywords"
        keywords = _clean_keywords(raw.get("keywords"))
        if activation == "keywords" and not keywords:
            result.rejected.append((index, "keywords activation needs at least one keyword"))
            continue
        if activation == "constant":
            keywords = []

        folded = name.casefold()
        current = live_by_id.get(int(item["target_entry_id"])) if op == "update" else None
        # An update keeping (or restoring) its own name is not a collision with itself.
        own_name = _clean_str(current.get("name")).casefold() if current else None
        if folded in taken_names and folded != own_name:
            result.rejected.append((index, f"a dynamic entry named {name!r} already exists in this world"))
            continue
        if own_name:
            taken_names.discard(own_name)
        taken_names.add(folded)

        item.update({"name": name, "content": content, "activation": activation, "keywords": keywords})
        result.operations.append(item)
        if target is not None:
            claimed_targets.add(target)

    return result


def parse_proposal_call(tool_calls: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Pull the ``propose_world_changes`` arguments out of parsed tool calls.

    Returns ``None`` when the model called something else or nothing at all; the
    last matching call wins, mirroring how the other forced steps read theirs.
    """
    found: Mapping[str, Any] | None = None
    for call in tool_calls:
        if call.get("name") == "propose_world_changes":
            args = call.get("arguments")
            if isinstance(args, Mapping):
                found = args
    return found


def describe_operation(op: Mapping[str, Any], entries_by_id: Mapping[int, Mapping[str, Any]] | None = None) -> str:
    """One human sentence for an operation, for logs and the review card fallback."""
    by_id = entries_by_id or {}
    target = op.get("target_entry_id")
    target_name = _clean_str(by_id.get(int(target), {}).get("name")) if target is not None else ""
    label = f"{target_name} [{target}]" if target_name else (f"[{target}]" if target is not None else "")
    kind = op.get("op")
    if kind == "create":
        return f"Add “{op.get('name', '')}”"
    if kind == "replace":
        return f"Replace {label} with “{op.get('name', '')}”"
    if kind == "suppress":
        return f"Suppress {label}"
    if kind == "update":
        return f"Update {label}"
    if kind == "archive":
        return f"Archive {label}"
    return str(kind)
