# Dynamic Worlds

After a turn, Orb can ask the Agent whether the shared World changed. The Agent
only proposes changes. A user reviews and accepts a pending changeset before it
can affect the lore.

The user-facing feature is [Dynamic Worlds](../features/dynamic-worlds.md).

## Two layers, one table

`lorebook_entries` stores both the user's authored lore and the Agent's overlay:

| | Authored | Dynamic |
|---|---|---|
| Owner | User | Agent, after user approval |
| Source | Drawer or import | Accepted changeset |
| Agent actions | None | Create, update, archive |

Dynamic entries never overwrite authored rows. The authored layer is therefore
the recoverable base; reset archives the live overlay and reveals it again.

An overlay row has one of three actions:

- `add` — new lore.
- `replace` — hides an authored entry and supplies replacement content.
- `suppress` — hides an authored entry without supplying content.

Archiving an overlay retires it without deleting it. The entry it hid becomes
visible again. Authored entries remain editable, and deleting one does not
delete its overlay; an orphaned replacement becomes standalone lore.

### The effective view

Callers must use `prompting/lorebook.select_effective_entries`, never the raw
pool. It:

1. removes disabled and archived rows;
2. removes authored rows hidden by a live `replace` or `suppress`;
3. removes `suppress` markers from the rendered result.

Dynamic entries follow authored entries under `Dynamic World State`. They use
the normal lorebook activation rules: `constant` for always-known facts and
keywords for local state.

## Proposing changes

`pipeline/world_proposal.py` runs after the editor and post-pipeline rewriting,
so it evaluates the text that will be saved. It does nothing when the Agent is
off, no enabled World has dynamic updates enabled, or the reply is empty,
aborted, or failed.

The stage:

1. rereads every enabled World that opted in;
2. makes one forced model call with the turn and a catalog of those Worlds;
3. validates the operations against the live entries;
4. splits valid operations into one pending changeset per World;
5. stages those changesets with the assistant message and emits one
   `world_change_proposed` event per changeset.

A failed or malformed proposal is logged and discarded. It never changes the
reply. A regenerated reply judges the original user message; steering text is
not treated as a world event.

The model sees a small vocabulary: `create`, `revise`, and `retract`. Validation
derives the storage action from the target row, so the model does not need to
know whether it is editing authored or dynamic lore. A revision inherits the
target's activation and keywords unless it supplies new values. Only a create
uses defaults.

`validate_proposal` checks the World, target, scope, body, and duplicate names.
It also repairs a keyword entry with no keywords by using the entry name. The
model never executes database CRUD.

## Review and apply

Applying a changeset takes the World lock and a SQLite `BEGIN IMMEDIATE`
transaction. The changeset's `base_revision` must still equal
`worlds.content_revision`:

- a match applies the complete batch;
- a mismatch marks the proposal `stale` and returns `409`.

There is no force-apply or automatic rebase. **Re-evaluate** derives a new
proposal from the stored source messages and the current World. The old one is
then marked `superseded`; a new proposal replaces it only if the new evaluation
has operations.

Other review actions use the same guarded path:

- editing operations is atomic and revalidated on the server;
- undo applies a compensating changeset only when the affected dynamic rows
  still have their recorded after-state;
- reset archives the live overlay and is itself undoable;
- manual deletes on an opted-in World are recorded in history as applied
  `manual` changesets.

`content_revision` advances once for each mutation that changes lore: entry
create/update/delete, bulk import, apply, undo, or reset. Renames and enable
flags do not invalidate pending proposals.

## History and visibility

Pending proposals are invisible to prompts, projections, and other characters.
Accepted lore becomes visible the next time a context is loaded. Worlds keep one
canonical timeline across conversation branches, so accepted lore remains even
if its source branch is later abandoned.

Source edits or deletion stale pending proposals. Applied history remains, with
nullable source references. Branch changes do not affect it.

Exports are authored by default. The effective view must be requested explicitly
(`?view=effective` or `?world_view=effective`). Preset backups include dynamic
entries and changeset history with the World.
