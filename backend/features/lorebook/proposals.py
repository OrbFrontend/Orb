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

**One call, several Worlds.** A turn may have more than one opted-in World in
play, so both halves take an optional *worlds* argument: the catalog groups its
entries under a heading per World, and validation resolves each operation to the
World it belongs in — from the target row for anything that names one (row ids
are globally unique, so that can never be guessed wrong), from ``target_world``
for a ``create``. Passing no *worlds* keeps the single-World shape, which is what
re-validating an already-scoped changeset on accept wants.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...inference.lorebook import (
    DYNAMIC_SECTION_TITLE,
    is_dynamic,
    select_effective_entries,
    select_keyword_entries,
)

# What a *stored* operation may say. The applier and the undo builder dispatch
# on these, so this is the vocabulary of the table, not the one the model is
# asked for.
OPERATIONS = ("create", "replace", "suppress", "update", "archive")
ACTIVATIONS = ("constant", "keywords")

# What the model is asked for (see ``PROPOSE_WORLD_CHANGES_TOOL``): three verbs,
# where `revise` and `retract` each cover two stored operations. Which one an
# operation becomes is decided by the layer of the row it targets, so the model
# is never asked to tell authored lore from the overlay -- a distinction it can
# only get from reading catalog headings, and one whose every wrong guess used
# to cost the whole operation. The stored names are accepted as synonyms: a
# changeset re-validated on accept comes back in the table's vocabulary, and a
# model that names one is not wrong, only more specific than it had to be.
_REVISE_OPS = ("revise", "replace", "update")
_RETRACT_OPS = ("retract", "suppress", "archive")
_TARGETING_OPS = frozenset(_REVISE_OPS + _RETRACT_OPS)

# How much of an entry's body the compact catalog line shows before eliding.
_COMPACT_CONTENT_CHARS = 90

# How much of an authored body the *full* rendering keeps, at each end, before
# dropping what is between them. Imported lore runs to thousands of characters
# and every constant entry is rendered full on every turn, so the middles of a
# few long entries are most of what a large World costs this step. The two ends
# are the parts it reads for: the opening says what the entry is, and the close
# is where an entry that has been added to over time carries its latest state.
_FULL_HEAD_CHARS = 400
_FULL_TAIL_CHARS = 200


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


def _world_key(world_id: Any) -> str:
    """A row's World as a comparable key. A row without one buckets under ``""``."""
    return str(world_id) if world_id is not None else ""


def _compact(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _COMPACT_CONTENT_CHARS else flat[: _COMPACT_CONTENT_CHARS - 1] + "…"


def _elide_middle(text: str, head_chars: int, tail_chars: int) -> str:
    """*text* with its middle dropped once it runs past ``head + tail`` characters.

    The gap is marked with the number of characters it stands for, because an
    unmarked cut reads as the whole entry: the step would take lore the entry
    already carries further down as missing, and propose a ``create`` for it.
    Cuts fall on whitespace so neither end stops mid-word, and *text* comes back
    untouched when the gap would not pay for its own marker.
    """
    if len(text) <= head_chars + tail_chars:
        return text
    # `rsplit`/`split` drop the partial word at each cut — and give it back
    # unshortened when the slice holds no whitespace to cut on at all.
    head_parts = text[:head_chars].rsplit(maxsplit=1)
    tail_parts = text[-tail_chars:].split(maxsplit=1)
    head = head_parts[0] if len(head_parts) == 2 else text[:head_chars].rstrip()
    tail = tail_parts[1] if len(tail_parts) == 2 else text[-tail_chars:].lstrip()
    omitted = len(text) - len(head) - len(tail)
    marker = f"…[{omitted} characters omitted]…"
    return f"{head} {marker} {tail}" if omitted > len(marker) else text


def _is_live_suppressor(entry: Mapping[str, Any]) -> bool:
    """True for a live ``suppress`` marker that is still hiding something.

    These are the one management-only row the catalog shows: they inject no lore,
    but the Agent has to be able to name one to archive it when later events make
    its authored target true again. A marker whose target has since been deleted
    has nothing left to hide (the delete SET-NULLs the pointer), so it is neither
    lore nor an actionable target -- listing it would only spend tokens on a row
    that cannot even say what it suppresses.
    """
    return (
        is_dynamic(entry)
        and entry.get("overlay_action") == "suppress"
        and entry.get("supersedes_entry_id") is not None
        and bool(entry.get("enabled", 1))
        and not entry.get("archived")
    )


def _entry_line(entry: Mapping[str, Any], *, full: bool) -> str:
    """One catalog line: id, name, activation, and body (full, elided or compacted)."""
    bits = []
    if entry.get("constant"):
        bits.append("constant")
    else:
        kws = [k for k in (entry.get("keywords") or []) if k]
        bits.append("keywords: " + ", ".join(kws[:5]) if kws else "no keywords")
    action = entry.get("overlay_action")
    if action == "replace" and entry.get("supersedes_entry_id") is not None:
        bits.append(f"replaces [{entry['supersedes_entry_id']}]")
    elif action == "suppress" and entry.get("supersedes_entry_id") is not None:
        bits.append(f"suppresses [{entry['supersedes_entry_id']}]")
    head = f"- [{entry.get('id')}] {entry.get('name', '')} ({'; '.join(bits)})"
    body = (entry.get("content") or "").strip()
    if not body:
        return head
    if not full:
        body = _compact(body)
    elif not is_dynamic(entry):
        # An authored body is only ever read here — no operation rewrites one —
        # so a long one can afford to lose its middle. A dynamic body cannot:
        # `update` rewrites content whole, and a middle the step never saw would
        # be written out of the World.
        body = _elide_middle(body, _FULL_HEAD_CHARS, _FULL_TAIL_CHARS)
    return f"{head}\n  {body}"


def _world_section(
    entries: Sequence[Mapping[str, Any]],
    relevant_ids: set[int],
    *,
    title: str = "",
) -> list[str]:
    """One World's catalog lines, split into its authored and dynamic sections.

    A titled section is always emitted, even with nothing in it: an empty World
    is still a legal ``target_world`` for a ``create``, so the step has to be
    told it exists. An untitled one (the single-World shape) renders nothing when
    it has no entries.
    """
    authored = [e for e in entries if not is_dynamic(e)]
    dynamic = [e for e in entries if is_dynamic(e)]

    parts: list[str] = [f"## {title}"] if title else []
    if authored:
        parts.append("### Authored")
        parts.extend(_entry_line(e, full=e.get("id") in relevant_ids) for e in authored)
    if dynamic:
        parts.append(f"### {DYNAMIC_SECTION_TITLE}")
        parts.extend(_entry_line(e, full=True) for e in dynamic)
    if title and len(parts) == 1:
        parts.append("(no entries yet)")
    return parts


def build_world_change_catalog(
    entries: Sequence[Mapping[str, Any]],
    *,
    worlds: Sequence[Mapping[str, Any]] = (),
    exchange_text: str = "",
) -> str:
    """Render the World(s) for the proposal step: every entry, ids attached.

    Dynamic entries always carry their full content — they are the rows the step
    may revise, so it must see exactly what they currently say. Authored entries
    are listed compactly unless they are *relevant to this exchange* (constant,
    or a keyword hit in the user message + reply), which keeps a large authored
    book from crowding out the exchange itself while still giving the step the
    text of anything it might contradict. A relevant authored body that is very
    long is shown from both ends with its middle elided
    (:func:`_elide_middle`): imported lore can run to thousands of characters
    per entry, and this step pays for every constant one of them on every turn.

    *entries* is the pooled row set of every World in *worlds*; each is rendered
    under its own ``## <name>`` heading, in the order given, so the step can name
    one in ``target_world``. With no *worlds* the pool is rendered flat, which is
    the single-World shape a re-evaluation wants.

    Suppressed authored entries, disabled rows, and archived overlay rows are
    absent: the step reasons about the World as it currently reads. Live
    suppression markers are the one management-only exception. They inject no
    lore, but remain listed with their own id so the Agent can archive one when
    later events make its authored target true again. Returns ``""`` when there
    is nothing at all to show.
    """
    effective = select_effective_entries(entries)
    effective_objects = {id(e) for e in effective}
    catalog_entries = [e for e in entries if id(e) in effective_objects or _is_live_suppressor(e)]
    if not catalog_entries and not worlds:
        return ""

    authored = [e for e in catalog_entries if not is_dynamic(e)]
    relevant_ids: set[int] = {e["id"] for e in authored if e.get("constant") and e.get("id") is not None}
    if exchange_text:
        scan = [{"role": "user", "content": exchange_text}]
        for e in select_keyword_entries(scan, [e for e in authored if not e.get("constant")], scan_depth=1):
            if e.get("id") is not None:
                relevant_ids.add(int(e["id"]))

    if not worlds:
        return "\n".join(_world_section(catalog_entries, relevant_ids))

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for e in catalog_entries:
        grouped.setdefault(_world_key(e.get("world_id")), []).append(e)

    parts: list[str] = []
    for world in worlds:
        world_id = _world_key(world.get("id"))
        name = " ".join(_clean_str(world.get("name")).split()) or "Unnamed lorebook"
        title = f"{name} [world_id: {world_id}]"
        parts.extend(_world_section(grouped.get(world_id, []), relevant_ids, title=title))
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


class _WorldScope:
    """Which Worlds an operation may land in, and how a ``create`` names one.

    Built once per proposal. With *worlds* given, a ``create`` must resolve to
    one of them (by name, or by id) and every accepted operation is stamped with
    its World for :func:`split_by_world`. With none given the scope is whatever
    single World *entries* came from, nothing is stamped, and ``target_world`` is
    ignored — the caller already knows which World it is re-validating.
    """

    __slots__ = ("_by_name", "ids", "stamped")

    def __init__(self, worlds: Sequence[Mapping[str, Any]], entries: Sequence[Mapping[str, Any]]):
        self.stamped = bool(worlds)
        if worlds:
            self.ids = [_world_key(w.get("id")) for w in worlds]
        else:
            seen = {_world_key(e.get("world_id")) for e in entries}
            self.ids = [seen.pop()] if len(seen) == 1 else [""]
        # A name shared by two Worlds names neither: resolving it would silently
        # write to whichever happened to sort first.
        counts = Counter(_clean_str(w.get("name")).casefold() for w in worlds)
        self._by_name = {
            _clean_str(w.get("name")).casefold(): _world_key(w.get("id"))
            for w in worlds
            if _clean_str(w.get("name")) and counts[_clean_str(w.get("name")).casefold()] == 1
        }

    def resolve_create(self, raw: Mapping[str, Any]) -> tuple[str | None, str]:
        """``(world_id, reason)`` for a ``create``; *reason* is set only on failure."""
        if len(self.ids) == 1:
            # One possible destination, so `target_world` carries no information
            # and cannot be wrong -- only unverifiable. Reading it anyway would
            # let a hallucinated name drop a create that had nowhere else to go.
            # (An unstamped scope is always this case, by construction.)
            return self.ids[0], ""
        named = _clean_str(raw.get("target_world"))
        if not named:
            return None, "create must name a target_world when more than one lorebook is listed"
        if named in self.ids:
            return named, ""
        resolved = self._by_name.get(named.casefold())
        if resolved is None:
            return None, f"unknown target_world {named!r}"
        return resolved, ""


def validate_proposal(
    arguments: Mapping[str, Any] | None,
    entries: Sequence[Mapping[str, Any]],
    *,
    worlds: Sequence[Mapping[str, Any]] = (),
) -> ValidatedProposal:
    """Vet a raw ``propose_world_changes`` argument dict against the live World(s).

    Also where the model's three verbs become the table's five operations: a
    ``revise``/``retract`` resolves to replace/suppress against an authored
    target and to update/archive against a dynamic one (the Agent may never
    modify or delete an authored row, so there is no operation that could). The
    layer comes from the row, never from the call, so the returned ``op`` is
    always one of :data:`OPERATIONS` whichever synonym came in.

    Every operation must survive all of:

    * a known ``op``;
    * a ``target_entry_id`` naming a live row in one of these Worlds, and — for
      an authored one — a row still in effect, since an entry already hidden by
      an overlay has nothing left to revise or retract;
    * one target per operation, and one operation per target — two operations
      aimed at the same entry are ambiguous, so the later one is dropped rather
      than guessed at;
    * a resolvable World: taken from the target row for anything that names one,
      and from ``target_world`` for a ``create`` when *worlds* lists more than
      one candidate;
    * non-empty name and content for anything that creates or rewrites an entry;
    * a name that does not collide (case-insensitively) with another live dynamic
      entry in the *same* World, or with an earlier create in it in this proposal.

    Keyword activation with no keywords is repaired rather than rejected — the
    entry's own name becomes its key — because an operation the user would have
    accepted should not be lost to a field the model can restate from another.

    *entries* is the pooled row set of every World in *worlds*; when *worlds* is
    empty the pool is one World's and operations come back unstamped (see
    :class:`_WorldScope`). Anything else is dropped with a reason. A proposal
    whose operations are all dropped is simply an empty proposal.
    """
    result = ValidatedProposal()
    if not isinstance(arguments, Mapping):
        return result
    result.summary = _clean_str(arguments.get("summary"))

    raw_ops = arguments.get("operations")
    if not isinstance(raw_ops, list):
        return result

    scope = _WorldScope(worlds, entries)
    effective = select_effective_entries(entries)
    # Two lookups, because the two layers of target mean different things.
    # `live_by_id` resolves the row an operation names — every enabled,
    # unarchived row, including suppression markers, since retiring one is how
    # the Agent brings a suppressed authored entry back. `by_id` is the narrower
    # test an *authored* target then has to pass: lore already hidden by an
    # overlay is not something a further operation can act on.
    by_id = {int(e["id"]): e for e in effective if e.get("id") is not None}
    live_by_id = {
        int(e["id"]): e for e in entries if e.get("id") is not None and bool(e.get("enabled", 1)) and not e.get("archived")
    }
    # Names that would collide, bucketed per World — two Worlds may each hold an
    # entry of the same name without ambiguity. Only *live* dynamic entries
    # count: an authored entry may legitimately share a name with the dynamic
    # row replacing it.
    taken_names: dict[str, set[str]] = {}
    for e in effective:
        if is_dynamic(e) and _clean_str(e.get("name")):
            taken_names.setdefault(_world_key(e.get("world_id")), set()).add(_clean_str(e.get("name")).casefold())
    claimed_targets: set[int] = set()

    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, Mapping):
            result.rejected.append((index, "operation is not an object"))
            continue
        op = _clean_str(raw.get("op")).lower()
        if op != "create" and op not in _TARGETING_OPS:
            result.rejected.append((index, f"unknown op {op!r}"))
            continue

        target = _target_id(raw)
        # The row a targeted operation names, once resolved; empty for a create.
        target_row: Mapping[str, Any] = {}
        if op == "create":
            if target is not None:
                result.rejected.append((index, "create must not name a target entry"))
                continue
            # The only operation whose World is not implied by a row, so the only
            # one that has to be told which lorebook it is writing to.
            world_id, reason = scope.resolve_create(raw)
            if world_id is None:
                result.rejected.append((index, reason))
                continue
        else:
            if target is None:
                result.rejected.append((index, f"{op} needs a target_entry_id"))
                continue
            row = live_by_id.get(target)
            if row is None:
                result.rejected.append((index, f"target entry {target} is not a live entry of any world here"))
                continue
            # The layer of the row decides which stored operation this becomes,
            # so a proposal never has to name the layer and can never name it
            # wrongly. An authored target must additionally still be *in effect*:
            # one already hidden by a live replace or suppress has nothing left
            # to revise or retract. (A dynamic row is legal while it is live,
            # including a suppression marker, which is not in effect by design.)
            dynamic_target = is_dynamic(row)
            if not dynamic_target and target not in by_id:
                result.rejected.append((index, f"target entry {target} is already hidden by an overlay row"))
                continue
            if target in claimed_targets:
                result.rejected.append((index, f"entry {target} is already targeted by an earlier operation"))
                continue
            if op in _REVISE_OPS:
                op = "update" if dynamic_target else "replace"
            else:
                op = "archive" if dynamic_target else "suppress"
            target_row = row
            # A targeted operation lands wherever its row already lives: entry
            # ids are globally unique, so this cannot be misdirected.
            world_id = _world_key(row.get("world_id"))

        item: dict[str, Any] = {"op": op, "rationale": _clean_str(raw.get("rationale"))}
        if scope.stamped:
            item["world_id"] = world_id
        if target is not None:
            item["target_entry_id"] = target
            # Snapshot what the target says *now*, so the review card can show a
            # before/after without a second query — and so applied history still
            # reads correctly once the live row has moved on. A proposal whose
            # World changed underneath it goes stale before it can be applied, so
            # the snapshot can never silently misrepresent what will happen.
            item["target_name"] = _clean_str(target_row.get("name"))
            item["target_content"] = _clean_str(target_row.get("content"))

        # `suppress` and `archive` carry no body: one hides an authored entry and
        # injects nothing, the other retires an overlay row it does not rewrite.
        # Both read their target's name off the row, which is why the schema
        # tells the model to omit `name` and `content` for a retract.
        if op in ("suppress", "archive"):
            if op == "suppress":
                # The marker inherits its target's name so the drawer and the
                # review card can say *what* was suppressed without a join.
                item["name"] = _clean_str(raw.get("name")) or _clean_str(target_row.get("name"))
            result.operations.append(item)
            if target is not None:
                claimed_targets.add(target)
            continue

        name = _clean_str(raw.get("name"))
        content = _clean_str(raw.get("content"))
        if op == "update":
            # An update may revise either field; whatever it omits keeps its
            # current value, so only a wholly empty update is meaningless.
            if not name and not content and "activation" not in raw and "keywords" not in raw:
                result.rejected.append((index, "update changes nothing"))
                continue
            name = name or _clean_str(target_row.get("name"))
            content = content or _clean_str(target_row.get("content"))
        if not name or not content:
            result.rejected.append((index, f"{op} needs both a name and content"))
            continue

        if op == "update":
            activation = _clean_str(raw.get("activation")).lower()
            if activation not in ACTIVATIONS:
                activation = "constant" if target_row.get("constant") else "keywords"
            keywords = (
                _clean_keywords(raw.get("keywords")) if "keywords" in raw else _clean_keywords(target_row.get("keywords"))
            )
        else:
            activation = _clean_str(raw.get("activation")).lower()
            if activation not in ACTIVATIONS:
                activation = "keywords"
            keywords = _clean_keywords(raw.get("keywords"))
        if activation == "keywords" and not keywords:
            # An entry that can never trigger is dead weight -- but the name is
            # almost always the thing the entry is *about*, so it is a usable key
            # and a better answer than dropping a reviewed proposal on the floor.
            keywords = [name]
        if activation == "constant":
            keywords = []

        folded = name.casefold()
        # An update keeping (or restoring) its own name is not a collision with itself.
        own_name = _clean_str(target_row.get("name")).casefold() if op == "update" else None
        taken = taken_names.setdefault(world_id, set())
        if folded in taken and folded != own_name:
            result.rejected.append((index, f"a dynamic entry named {name!r} already exists in this world"))
            continue
        if own_name:
            taken.discard(own_name)
        taken.add(folded)

        item.update({"name": name, "content": content, "activation": activation, "keywords": keywords})
        result.operations.append(item)
        if target is not None:
            claimed_targets.add(target)

    return result


def split_by_world(operations: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """Group stamped operations by World, dropping the stamp.

    One ``propose_world_changes`` call may touch several Worlds, but a changeset
    belongs to exactly one — each has its own ``content_revision`` to race
    against and its own review queue. This is the split, and it is also where the
    transient ``world_id`` key :func:`validate_proposal` stamps comes off, so
    what reaches the database is the same operation shape as ever.

    Worlds come back in first-touched order; an unstamped operation is dropped,
    since there is no World it could be filed under.
    """
    grouped: dict[str, list[dict]] = {}
    for op in operations:
        item = dict(op)
        world_id = _clean_str(item.pop("world_id", ""))
        if world_id:
            grouped.setdefault(world_id, []).append(item)
    return grouped


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
