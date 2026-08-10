# Dynamic Worlds

The Agent may propose that a finished turn changed something durable about the
shared World. It never applies anything: a proposal is a **pending changeset**
the user reviews, and only their acceptance touches the lore.

V1 is review-only. Automatic application is deliberately deferred.

---

## 1. Two layers, one table

`lorebook_entries` holds both layers.

| | `entry_layer = 'authored'` | `entry_layer = 'dynamic'` |
|-|----------------------------|---------------------------|
| Owner | the user | the Agent |
| Written by | the lorebook drawer, import | an accepted changeset |
| The Agent may | *nothing* — never modify, never delete | create, update, archive |

Because the overlay never writes an authored row, **the authored rows are the
recoverable original**. That single fact is why there is no baseline snapshot
table and no per-entry UUID scheme: "Reset to Authored World" archives every
live overlay row and the authored view falls back out, deterministically.

An overlay row carries an `overlay_action`:

- **`add`** — new lore, standing on its own.
- **`replace`** — hides the authored entry named in `supersedes_entry_id` and
  injects itself instead.
- **`suppress`** — hides its target and injects *nothing*. It is a marker, not
  content.

`archived = 1` retires an overlay row without deleting it, which re-exposes
whatever it was hiding. Archiving is therefore both "undo" and "reset".

> "Original" here means *the user-owned authored layer before Agent overlay*,
> not an eternal snapshot of the first imported file. Authored entries stay
> freely editable; preserving every historical user edit would be a separate
> authored-versioning feature.

### The projection

`inference/lorebook.select_effective_entries` resolves the pool:

1. drop archived rows;
2. drop authored entries hidden by a live `replace` or `suppress`;
3. drop `suppress` markers themselves.

**Nothing downstream may see the raw pool.** Rather than trusting each caller to
remember, every activation and rendering entry point in `inference/lorebook.py`
applies the projection as its own first step — `select_active_entries`,
`build_lorebook_catalog`, `compute_constant_lorebook_block`,
`compute_depth_lorebook_block`. That is also what keeps the pipeline's cached
prefix and the workflow toolkit's off-turn prefix byte-identical: they call the
same function with the same raw rows and cannot diverge.

Dynamic entries render *after* authored ones, under a `Dynamic World State`
heading, in every block. Activation is hybrid and uses ordinary lorebook
mechanics: `constant` for truths that must be known every turn, keywords for
state about one entity or place.

---

## 2. `content_revision`

`worlds.content_revision` is an optimistic-concurrency stamp, not a counter.
It advances **once per atomic mutation that changes what the lore says**:

- authored entry create / update / delete,
- a bulk import (once for the whole file — hence
  `import_lorebook_entries` doing the whole book in one transaction),
- a changeset apply / undo / reset.

It deliberately does **not** advance for a rename, `enabled`, or
`dynamic_enabled`. The character-switch flow toggles `worlds.enabled`, and that
must never invalidate a pending proposal.

---

## 3. The turn stage

`pipeline/world_proposal.py` runs last in `_run_pipeline` — after the editor and
after the draft-rewriting post-pipeline hooks — so the prose it judges is the
prose that will be persisted.

It is skipped when: the Agent is off, there is no character or linked World, the
linked World has `dynamic_enabled = 0`, or the reply is empty, aborted or failed.

Other properties worth not breaking:

- **The target is the conversation's linked character card's World, and nothing
  else.** Never the set of globally enabled Worlds — that is a display concern,
  and inferring from it would let one character's events land in another
  World's lore.
- **The World is re-read immediately before the call**, so the proposal names the
  revision as it stands *after* the turn's own latency.
- **A steered regeneration judges the original user message.** Orb's OOC steering
  prompt directs the writer; it is not an event in the world.
- **A failed or malformed proposal call costs nothing.** The reply is already the
  user's; every failure path leaves `TurnState.world_proposal` as `None` and is
  logged.
- The proposal is staged as a pending changeset at the same persistence boundary
  as the assistant message (it names that message), and
  `world_change_proposed` is emitted before `done`.

The `propose_world_changes` schema chooses only between `constant` and
`keywords` activation; the other lorebook fields keep safe defaults the user can
edit afterwards through the normal reviewed path. Keeping the schema small keeps
the shared per-turn tool blob small and stable (see
[kv-cache.md](kv-cache.md)).

The model never executes CRUD. `features/lorebook/proposals.validate_proposal`
turns its call into normalised operations or rejects them, checking every claim
against the live World — layer, scope, one target per operation, non-empty body,
at least one keyword under keyword activation, no ambiguous duplicate dynamic
names.

---

## 4. Deciding

Applying takes a per-World process lock (`core.locks.world_apply_lock`) **and** a
SQLite `BEGIN IMMEDIATE` transaction that re-reads `content_revision` inside it.
The proposal's `base_revision` must match exactly.

On mismatch: nothing is applied, the proposal is marked `stale`, and the route
returns `409`.

**There is no force-apply and no automatic rebase.** Two changes that touch
different entries can still contradict each other in meaning, so the remedy is
**Re-evaluate**: re-derive a fresh proposal from the stored source messages
against the World as it now stands.

Everything else is expressed through the same path:

- **Editing before applying** is atomic — the user may reword or remove
  individual operations, but the surviving batch commits together, and the
  server re-validates rather than trusting the client.
- **Undo** builds a compensating changeset and applies it, but only while every
  affected dynamic entry still matches the recorded after-state; otherwise
  `409`. It never clobbers a later edit.
- **Reset** is itself an undoable changeset.

Source changes:

| Event | Pending proposals | Applied history |
|-------|-------------------|-----------------|
| Either source message edited | → `stale` | untouched |
| Source message or conversation deleted | → `stale` (found by orphaned `source_assistant_message_id`, since the cascade NULLs it before app code could match ids) | kept, with nullable source ids and denormalised labels |
| Branch switched | untouched | untouched |

A World has one canonical timeline, independent of conversation branches. Once
accepted, a change stays shared canon even if its source branch is later
abandoned or regenerated.

---

## 5. Visibility

- A pending proposal is invisible to everything: the projection, the prompt, and
  every other character sharing the World.
- Acceptance makes it visible to a character sharing the World **on the next
  generation whose context loads after acceptance**. No real-time cross-tab
  synchronisation is promised.
- Export stays **authored by default** — both the standalone lorebook export and
  the embedded `character_book` in a card export. Exporting the effective state
  is explicit (`?view=effective`, `?world_view=effective`).
- Orb preset backups carry dynamic entries and changeset history automatically:
  `world_changesets` hangs off `worlds` via `ON DELETE CASCADE`, so the
  schema-driven preset engine places it in the `lorebooks` domain with no policy
  edit, and its message/conversation pointers are ordinary nullable crossrefs.
