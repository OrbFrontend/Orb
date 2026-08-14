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

**Deleting an authored entry never deletes the overlay on top of it.**
`supersedes_entry_id` is `ON DELETE SET NULL`, so a hard delete of the authored
row drops only the pointer: a `replace` that no longer hides anything becomes a
standalone `add`, and a `suppress` with nothing to suppress goes inert (the
projection already drops every marker, and the proposal catalog stops listing an
orphaned one). Accepting a `replace` is exactly what makes the authored row look
redundant, so this is the *likely* cleanup, not a corner case — and it must not
silently discard lore the user reviewed and accepted. Undo tolerates this one
transition specifically (see §4); every other field still guards. The delete
itself lands in History as a `manual` changeset, so the pointer going `NULL` has
a recorded cause rather than looking like the overlay drifting on its own.

> "Original" here means *the user-owned authored layer before Agent overlay*,
> not an eternal snapshot of the first imported file. Authored entries stay
> freely editable; preserving every historical user edit would be a separate
> authored-versioning feature.

### The projection

`inference/lorebook.select_effective_entries` resolves the pool:

1. drop disabled or archived rows;
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

It is skipped when: the Agent is off, no enabled World has `dynamic_enabled = 1`,
or the reply is empty, aborted or failed.

Other properties worth not breaking:

- **The targets are every *enabled* World that opted in** (`predicates.world_proposal_active`),
  not the conversation's linked character card's World. Both halves of that gate
  matter: `dynamic_enabled` is the user handing the Agent a pen, and `enabled` is
  what makes the exchange evidence at all — an enabled World's lore is what fed
  this turn's prompt, while a disabled one contributed nothing to the scene and
  so learns nothing from it. A card's `world_id` is a *linking* concern; the lore
  actually in play is the enabled set, and several Worlds are routinely in play
  at once.
- **One call, one changeset per World.** The Worlds in play share a single forced
  call — the judgement is about the exchange, not about a book, and asking once
  per World would cost N generations to answer the same question — and its
  catalog groups entries under a `## <World name> [world_id: <id>]` heading each.
  Operations that
  name a target row take that row's World (entry ids are globally unique, so
  that cannot be misdirected); a `create` puts the stable id shown in the heading
  into `target_world`, required only when the catalog lists more than one. This
  stays unambiguous even when two Worlds share a display name.
  `proposals.split_by_world` then
  files the validated operations into one pending changeset per World, because
  that is the unit both the revision race and the review queue work in.
- **Every World is re-read immediately before the call**, so each proposal names
  the revision as it stands *after* the turn's own latency, and a World whose
  opt-in (or `enabled`) was toggled off mid-turn drops out without disturbing the
  others.
- **A steered regeneration judges the original user message.** Orb's OOC steering
  prompt directs the writer; it is not an event in the world.
- **A failed or malformed proposal call costs nothing.** The reply is already the
  user's; every failure path leaves `TurnState.world_proposals` empty and is
  logged.
- The proposals are staged as pending changesets at the same persistence boundary
  as the assistant message (they name that message), and one
  `world_change_proposed` event per changeset is emitted before `done`. One
  World failing to stage never drops another's.

The `propose_world_changes` schema chooses only between `constant` and
`keywords` activation; the other lorebook fields keep safe defaults the user can
edit afterwards through the normal reviewed path. Keeping the schema small keeps
the shared per-turn tool blob small and stable (see
[kv-cache.md](kv-cache.md)) — which is also why `target_world` is a fixed field
of that schema rather than one narrowed to the Worlds of the moment.

**The model's vocabulary is not the table's.** The schema offers three verbs —
`create`, `revise`, `retract` — while the table stores five. Whether a revise or
retract lands as `replace`/`suppress` (authored target) or `update`/`archive`
(dynamic target) follows entirely from `target_entry_id`, so `validate_proposal`
reads it off the row. Asking the model instead would mean asking it to classify
a target's layer from catalog headings, and paying for every wrong guess with a
dropped operation. The stored names stay valid input — accepting a changeset
re-validates operations that are already in the table's vocabulary. Every field
the schema keeps is one the model alone can supply; anything derivable from the
World is derived, because a field that can disagree with the World is a field
that can lose a proposal the user would have accepted.

**Re-evaluate stays single-World.** A changeset belongs to one World, so
re-deriving it re-runs the step against that World alone: the user is re-judging
*this* proposal, and opening a second World's queue from that click would be a
surprise.

The model never executes CRUD. `features/lorebook/proposals.validate_proposal`
turns its call into normalised operations or rejects them, checking every claim
against the live World — the target is live (and, when authored, still in
effect), scope, one target per operation, non-empty body, no ambiguous duplicate
dynamic names. Keyword activation with no keywords is repaired, not rejected:
the entry's own name becomes its key.

The proposal catalog is the one projection-adjacent management view: it also
lists live `suppress` markers (which inject no lore) so the Agent can name and
archive one if later events make its authored target true again.

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

Re-evaluation atomically moves the original to terminal `superseded` and, when
the new judgement still has operations, inserts its replacement in the same
transaction. If a concurrent apply, reject, or second re-evaluation wins first,
the losing attempt inserts nothing. An evaluation that finds nothing also
supersedes the original, so the resolved item leaves the review queue.

Everything else is expressed through the same path:

- **Editing before applying** is atomic — the user may reword or remove
  individual operations, but the surviving batch commits together, and the
  server re-validates rather than trusting the client.
- **Undo** builds a compensating changeset and applies it, but only while every
  affected dynamic entry still matches the recorded after-state; otherwise
  `409`. It never clobbers a later edit. The single tolerated difference is a
  `supersedes_entry_id` that has gone `NULL`: that is the user's own delete of
  the authored target showing through the pointer, not an edit to the overlay
  row, and refusing on it would strand the changeset with an Undo button that
  could never succeed.
- **Reset** is itself an undoable changeset.
- **Deleting an entry by hand is recorded**, on a Dynamic World, as an
  already-applied changeset of `origin = 'manual'` carrying one `delete`
  operation and the row's before-snapshot — written on the same transaction as
  the `DELETE`, so history claiming a delete that did not happen is unreachable.
  It is the one drawer mutation that leaves nothing behind (the row is gone, and
  every applied changeset that touched it stops being undoable), so without a
  record History would show the Agent's removals and silently omit the user's.
  A `delete` is the only stored operation with no inverse — the applier never
  dispatches on it, `invert_operations` finds nothing to compensate with, and
  the review surface offers no Undo button rather than one the server can only
  refuse. A World that never opted in records nothing: it has no history for the
  row to join.

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
