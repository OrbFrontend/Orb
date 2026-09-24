# Persistent state fragments

## Goal

Give Orb a reliable, generic way to remember changing conversation state. Merge
direction notes and progressive fragments into one **state** fragment with two
state modes: **one current value** or **multiple entries**. Authors describe
what to remember in plain language; Orb handles persistence, branch history,
and injection. Decision fragments stay independent and can inform updates.

Examples include scene continuity, a character's current understanding, open
promises, inventory, a writing project's established canon, and an evolving
style preference. None needs a domain-specific state type.

## Scope and behavior

Each state fragment uses the existing fragment ID, name, description, enabled
flag, order, and cooldown. Add only these choices:

- **Mode:** one value or multiple entries.
- **Update:** after the completed reply (default), before Writer, or manual only.
- **Inject:** off, Director, Writer, or both. Injection and automatic updating
  remain independent.

Both modes use the same entry storage and event history, and events record
explicit entry writes and retirements, so replaying history never depends on
the mode. Mode is an ordinary fragment setting, like the fragment type is
today: editable at any time, the same for every conversation and branch, and
effective on the next turn. It decides which operations the model and the
State panel offer and how the value is rendered, not how history is read. The
model cannot change it.

Changing the mode never rewrites or discards state. Switching to multiple
entries keeps the existing entry and its ID. Switching to one value leaves any
active entries in place. One-value mode tolerates several active entries
wherever they arise: it renders them together as the current value, so the
Director, Writer, and updater see everything, and the State panel notes that the
next update replaces them with one value. The user can merge them first with
the ordinary revise and retire actions. Otherwise the model's merge is an
ordinary model-made change, shown in the Inspector, and the originals stay in
history as retired entries.

The updater always sees the current state of the fragments it updates; Inject
controls only what the other passes see. A fragment on cooldown keeps and still
injects its value, as a resting progressive fragment does now. A before-Writer
one-value update rides the
Director's `direct_scene` call, so when the Agent or `direct_scene` is off the
fragment keeps its value, and the editor says so.

The fragment editor explains the timing choice by its effect: an after-reply
update records what the reply actually showed; a before-Writer update records
the Director's intent for this reply, which the Writer may not carry out. A
disabled fragment is neither updated nor injected; the State panel still shows
its saved state read-only. The existing global direction-note **Recording**
switch becomes a global **State updates** switch, so one toggle still stops
every automatic update call.

The model gets the smallest set of operations that covers both modes. For one
value it sets new text; for multiple entries it adds new entries or retires
existing ones, and a revision is a retire plus an add. Omission means `keep`:
an omitted fragment, skipped pass, invalid tool result, cooldown, or model
failure leaves the existing state intact, and empty text never clears or
retires anything. The model cannot clear a one-value fragment; it sets a value
such as "none", which keeps `direct_scene`'s fixed parameters unchanged. The
user has two more operations in the State panel: `clear` a value and `revise` an
entry in place. Reject unknown fragment IDs, unknown entry aliases, and
oversized values before writing. Use fixed limits for value length and active
entry count, not another set of user-facing controls. The updater sees each
fragment's active count and the limit. An add beyond the limit is rejected,
reported in the Inspector, and marks the fragment as full in the State panel: a
small model that never retires would otherwise stop recording with nobody
noticing.

The updater receives the fragment instructions, its current state, the relevant
turn, and any resolved decision outcome. A before-Writer update can guide that
reply; an after-reply update reads the final text that will be saved and affects
later turns. A resolved Judge outcome is context, not a new state schema or an
automatic write. Do not run another Judge evaluation for state.

State belongs to the active conversation branch. A regeneration starts from
its parent path, and another branch retains its own state. Group chats update
shared state once per exchange, following the existing direction-note behavior.
Manual edits produce changes on the active branch, anchored to the active leaf.
Regenerating a reply carries the user-made changes anchored on it to the new
reply and applies them before generation, so the new reply is written with the
correction; model-made changes are regenerated. A carried `revise` or `retire`
whose entry was added by the discarded reply has nothing to apply to; drop it
and report it in the Inspector. Keep the change source and message anchor so
the panel and Inspector can explain a current value. Disabling a fragment stops
updates but does not delete its saved state.

Capture the fragment configuration, including modes, once per turn, and use
that same contract for tool construction, routing, validation, and commit.
Serialize generation and manual state writes per conversation on the server;
reject a conflicting manual write as busy rather than applying it mid-turn.
Fragment-setting edits take effect on the next turn. Never reinterpret an
in-flight string result as an array because a setting changed.

A stopped reply that is saved as partial text keeps the before-Writer changes
that shaped it; after-reply changes never run for it. (Today a partial save
loses its progressive values, so the next turn starts from empty.) A turn that
saves no reply persists no changes.

Checkpoint copies the active path, so it copies the path's events too and remaps
their anchors through the message-ID map it already uses for decision anchors;
history and sources survive. Compress History drops most messages: write the
state folded up to the kept tail as `carried` events on the summary message,
then copy the tail's own events onto the re-added messages, so regenerating the
first kept reply does not re-apply that reply's changes. Today Checkpoint drops
direction notes, and Compress drops both direction notes and progressive values.

### Intentional behavior changes

Converted data keeps each branch's current state, but these behaviors change on
purpose and belong in the feature docs:

- Omitting a one-value fragment keeps its value. Before, an omitted progressive
  value was dropped.
- A fragment that injects into the Writer shows its current value on every turn.
  Before, the Writer saw a progressive value only on turns the Director emitted
  it. Check the added prompt size with every limit at its maximum.
- A partial save keeps its before-Writer changes.
- Direction notes can be retired and revised, not only added.
- Switching a fragment between one value and multiple entries keeps its saved
  state. Before, switching a fragment away from progressive hid its values.
- Legacy direction-note fragments embedded in cards follow the global switch and
  inject into both passes. Under the old default settings (recording and
  injection off) they did nothing.

## Implementation

1. **Add one state history and projection.** Add a small `fragment_state_events`
   table with an autoincrement ID, conversation, anchor message, fragment ID,
   and entry ID when applicable. Store the operation, text or mode when
   applicable, the fragment label at write time, source (`agent`, `user`, or
   `carried`), and timestamp. Several events may share an anchor and entry; the
   row ID orders them within an anchor. The denormalized label keeps state
   readable after a fragment is renamed or deleted, as direction notes do today.
   Both modes use stable IDs per entry. A one-value `set` retains the sole active
   entry's ID, creates one if empty, or retires every active entry and adds one
   when several are active; a clear retires every active entry. Translate public
   operations into explicit entry writes and retirements before persistence, so
   their replay never depends on the fragment's current configuration.

   Fold events in active-path order to obtain the active entries. The fold knows
   nothing about modes. Keep it pure; put SQL and row models in `database/`.
   Commit generated events with the assistant reply, including the partial-save
   path, so a saved reply and its state cannot diverge. On a regenerated reply,
   insert the carried user events before the generated ones so row order matches
   apply order. Add one snapshot helper that folds a path and writes its entries
   as `carried` events on a message, for Compress History; Checkpoint remaps
   events instead. Enforce the conversation write serialization at the server
   boundary, not only by disabling UI controls.

2. **Use one operation contract.** Replace the direction-note recording step and
   the progressive-field update path with one validator and applier for the
   operations above. Model output is limited to those operations; code validates
   and applies them using the turn's captured configuration. The timings keep
   today's transports, so migration adds no model calls; a later mode or timing
   change can change which transport is needed:
   - Before-Writer one-value fragments ride the Director's `direct_scene` call,
     as progressive fragments do now.
   - Before-Writer multiple-entry fragments use the existing post-Director step.
   - After-reply fragments use the existing post-turn call that replays the
     Writer exchange onto the warm prefix.

   Keep the model-facing shape close to today's one-string-per-fragment tools,
   which small models already handle:
   - A one-value fragment is one string parameter, in `direct_scene` before the
     Writer or in the state tool after the reply. Omitted or empty means keep.
   - A multiple-entry fragment is one array-of-strings parameter holding new
     entries.
   - The state tool has one fixed `retire` parameter, an array of entry aliases,
     placed before the fragment parameters. Aliases are numbered across the
     whole request (`e1`, `e2`, ...), so one list serves every fragment in the
     call; the per-fragment-call option numbers them per call.

   Benchmark this shape on at least two small models before building the UI.
   Measure the valid-call rate, whether retires happen as a list nears its limit,
   and how often correct entries are retired by mistake.

   Reuse the Agent lane and the per-fragment-call option. Tool schemas ride the
   shared cached tools blob, so their shape stays fixed while the fragment
   configuration is unchanged. Editing a fragment's settings, including its
   mode, rebuilds the schema on the next turn, as editing a fragment does today;
   state writes and branch switches must not. Never put entry IDs, current
   values, or state revision numbers in a schema. List active entries in the
   request text under their aliases and map them back in code. Render one
   compact current-state block for each configured recipient; a one-value
   fragment with several active entries renders them as a list. On the turn a
   before-Writer update changes a one-value fragment, render it as `old -> new`
   for the Writer, as progressive fragments do now. A before-Writer update goes
   into the current turn's Writer block; after-reply updates become visible on
   the next turn.

   This is an explicit contract update. It renames `record_direction_note` in
   `BUILTIN_TOOL_ORDER` and its `enabled_tools` key, and replaces the
   `direction_notes` SSE step and event. Update
   [prompting](../architecture/prompting.md) and [SSE](../architecture/sse-stream.md)
   in the same change, and keep every other ordering and stream invariant.

3. **Convert existing data without changing what users see.** Add a migration
   that converts existing fragments with explicit settings, because the new
   defaults would change their behavior:
   - `progressive` becomes one value, Update **before Writer**, Inject **both**:
     the Director sees the prior value today and the Writer sees `old -> new`.
     The after-reply default would add a model call and move the update past the
     Writer.
   - `direction_note` becomes multiple entries, with Update from
     `direction_note_timing` and Inject from `direction_notes_inject`.
   - The global State updates switch starts **on**. Where `direction_notes_record`
     was off, the converted direction-note fragments get Update **manual only**.
     Recording defaults to off and never gated progressive updates, so copying
     it to the global switch would stop progressive updates for most users.
   - The `enabled_tools` key maps to the new tool name.

   Convert the data so every branch tip keeps its current state:
   - Each assistant message stores a full progressive snapshot. Walk the
     assistant messages of the tree and write a `set` event only where a value
     differs from the parent path, retaining the active entry ID. Retire the
     active entry where a value disappears (the public `clear` operation),
     because under the old rules an omitted value was dropped. Skip a snapshot
     that is entirely empty: partial saves and turns where `direct_scene` did not
     run wrote `{}`, and converting those to `clear` would re-create the
     partial-save loss this plan fixes.
   - Convert saved direction notes to anchored `add` events.
   - Move user-authored notes (the `"human"` sentinel fragment ID) into a seeded,
     ordinary state fragment named **Notes**: multiple entries, manual only,
     Inject from `direction_notes_inject`. Prefix each note's text with its old
     label unless the label is the default "Note". New installs get the same
     fragment, disabled, so free-form notes stay available without a special
     group or a per-entry label.

   The migration also runs on imported `.db` backups and presets, so it must
   work when a snapshot holds only some domains, such as chats without
   fragments. Card fragments are not rewritten: shared card files keep the old
   types indefinitely, so `card_embedded_fragments` maps them to state fragments
   at read time, permanently. `progressive` gets the settings above;
   `direction_note` gets multiple entries, its own timing, and Inject both,
   because cards never carried an inject setting. Card export writes the new
   type with explicit settings; older Orb versions read an unknown type as a
   plain string field. Stop writing the old columns and the `direction_notes`
   table in this change. Drop them in a follow-up cleanup migration once the
   conversion check has passed on real data. Write that check as a standalone
   script so it can be re-run on each real database.

4. **Make the smallest UI change.** Rename the existing Notes panel to
   **State** and show fragment names with their current value or active entries.
   Show its button whenever an enabled state fragment exists or the conversation
   holds saved state; today the button hides unless recording or injection is
   on. Let users set or clear a value and add, revise, and retire an entry, with
   a compact history view. Mark a full fragment, and show a deleted fragment's
   state read-only under its saved label with an option to delete it. A
   one-value fragment with several active entries lists them with a note that
   the next update replaces them with one value, and keeps revise and retire
   available so the user can merge them first. Extend the existing fragment
   editor with the three choices above, replacing the progressive and
   direction-note type options. Keep disabled and deleted fragments read-only.
   Seed two disabled starter fragments that show the two modes in
   use: **Scene continuity** (one value, before Writer, Writer) and
   **Open threads** (multiple entries, after reply, both). Reuse the current
   panel and settings layout, and update the panel's selectors in the bundled
   themes and `mobile.css`. Do not add a dashboard, a separate decision builder,
   or per-turn state cards. Show generated changes, rejected operations, and
   dropped carried corrections in the Inspector, and refresh the State panel
   after a turn.

5. **Update the public documentation and remove the old paths in the same
   change.** The bundled frontend is the only consumer of the direction-note
   endpoints, so replace them with state endpoints rather than routing them
   through a compatibility layer. Old data from outside Orb arrives through the
   migration (backups, presets) or the card read boundary (card files) only.
   Explain what a mode switch does to existing state, the timing choice, `keep` semantics, and the intentional behavior changes in
   the feature docs. Replace
   `direction-notes.md` and the Progressive row in `director.md`, and update
   `card-fragments.md`.

## Checks that matter

- A state fragment can express scene continuity, a single evolving value, and
  multiple open items without special cases in the schema or UI.
- Switching a fragment's mode writes no events and folds the same entries.
  Switching to multiple entries keeps the entry's ID. A one-value fragment with
  several active entries shows all of them to every recipient and in the State
  panel; its next `set` retires them all and adds one, and the originals remain
  in history.
- `keep`, a skipped call, and a malformed call preserve the prior state; only
  explicit operations change it. An over-limit add is rejected and the fragment
  shows as full.
- The benchmark shows small models produce valid calls and retire entries
  before the tool shape is committed to the UI.
- Regenerating, editing, and switching branches produce the right current state.
  A manual correction survives regenerating the reply it sits on and is visible
  to the regenerated reply; a carried correction whose entry no longer exists is
  dropped and reported. Group exchanges update state once. A stopped, partially
  saved reply keeps its before-Writer state.
- Mid-turn configuration edits do not change the captured contract, and
  concurrent manual state writes are rejected server-side.
- The Writer sees before-Writer changes on the same reply and after-reply
  changes on the next reply. A Judge outcome is available to the updater but
  does not itself mutate state.
- Legacy migration leaves every branch tip's state unchanged: for each leaf,
  the folded state equals the legacy progressive value and direction notes on
  its path, except where the empty-snapshot rule restores a value a partial save lost.
  Run this against a real database, not only fixtures. Card fragments, older
  backups, and partial presets load correctly.
- After conversion with recording off, progressive fragments still update and
  converted direction-note fragments make no model call.
- Checkpoint starts the new conversation with the source's state and history.
  Compress History preserves the state, and regenerating the first kept reply
  does not apply that reply's changes twice.
- The tools blob stays byte-identical across turns and branch switches while
  state values and entries change under unchanged fragment configuration. A
  mode change rebuilds the schema before the next call.
  A conversation with only migrated progressive fragments makes no more model
  calls per turn than before unless the user changes its configuration.
- Run the narrow integration tests for state, branch regeneration, group chat,
  and cache invariants, then the repository's format, lint, and test scripts.

This plan intentionally leaves out typed facts, stages, counters, transition
graphs, semantic deduplication, author-defined initial values, and automatic
promotion into Dynamic Worlds. The snapshot helper makes initial values cheap to
add later if real fragments need them.
