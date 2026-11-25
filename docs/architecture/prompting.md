# Prompting boundary

`backend/prompting/` owns deterministic, provider-independent model-facing
construction. A module belongs there only when it:

1. deterministically transforms caller-supplied data;
2. constructs model-facing content or a canonical projection required by it;
3. is shared by multiple upper-layer consumers or the stable workflow toolkit;
4. performs no I/O, model call, persistence, or feature/pass enablement decision.

Instruction prose and result contracts used by one pipeline pass stay with that
pass. Feature enablement stays in its feature slice. Provider adaptation,
retries, structured-output normalization, cache mechanics, and local runtimes
stay in `inference/`.

## Allowed dependency graph

```text
api        -> pipeline, features, workflows, prompting, inference, analysis, database, core
pipeline   -> features, workflows, prompting, inference, analysis, database, core
features   -> prompting, inference, analysis, database, core
workflows  -> prompting, inference, analysis, database, core
prompting  -> core
inference  -> core
analysis   -> database, core
database   -> core
core       -> (nothing)
```

`features/` and `workflows/` are siblings and do not import one another.
Feature slices do not import peer slices. The `workflows` row applies to the
host framework modules directly under `backend/workflows/`; plug-in slices
under `backend/workflows/<id>/` may import only their own package and the public
workflow toolkit, never these lower layers, host internals, or peer plug-ins. The exact rules are
enforced by `scripts/check_backend_layers.py`; every Python-bearing top-level
backend package must be classified there.

Shared sentence boundaries, quotation scanning, and protected-markup primitives
belong in `core/text_segmentation.py`, which both inference and analysis use.
Roleplay block segmentation (speech, emphasis, narration, and OOC asides) belongs
in `analysis/text/roleplay_segmentation.py` and builds on those primitives.
Consumers import core helpers directly; the analysis module does not re-export
them. Workflow plug-ins access shared capabilities through the stable toolkit.

## Byte and order contracts

Prompt text, whitespace, delimiters, message order, parameter order, macro
resolution timing, seeds, and stored random choices are public behavior and must remain byte-for-byte stable.

Built-in tools use this order:

```python
(
    "direct_scene",
    "editor_apply_patch",
    "editor_rewrite",
    "editor_find_replace",
    "give_feedback",
    "update_state",
    "select_lorebook",
    "propose_world_changes",
)
```

Enabled built-ins preserve that order. Workflow tools append in registration
order, which is plug-in package-name order, and re-registering a workflow tool
preserves its position. Schema property order and insertion-order-preserving
JSON serialization are part of the transport contract.

## Card message projections

History formatting resolves identity macros, then applies caller-supplied
`CardScripts` for the prompt channel to the message body, then appends attachment
annotations and group speaker labels. Stored bodies, system instructions, and
card definitions remain canonical. Group history uses the original speaker's
card scripts, independent of the currently generating speaker; unattributed
rows have no owning card. Pipeline and off-turn workflow prefixes share this
contract. See [Card render scripts](../features/card-render-scripts.md).

Workflow-owned text composed outside that prefix -- saved configuration a
workflow reuses in every conversation -- resolves against the same identity
through `toolkit.conversation_macros`, which shares the prefix's persona-lock
precedence so one call cannot name the user two ways. Such text rides a per-call
tail rather than the shared prefix, so it may pass `fresh=True` and roll
`{{random}}` and `{{roll}}` fresh each call without affecting prefix reuse; a
`{{pick}}` stays fixed for the conversation.

## Prompt layout

A prompt layout is an ordered list of blocks around a fixed **Chat history**
divider. `core/prompt_layout.py` holds the vocabulary (block shape, marker ids,
section placeholders, and the blocks a new layout seeds); `prompting/layout.py`
renders it. Chat mode reads the active layout; Document mode and the Assistant
panel do not.

- **Prefix** blocks (above the divider) build the system message and optional
  pre-history messages. Every pass and off-turn workflow call reads them.
- **Writer request** blocks (below the divider) build the Writer's messages
  after history. The Editor and the later passes replay those messages; the
  Director never sees them. Continue sends no Writer request, so these blocks do
  not steer it.

A **marker** stands for an engine block: `main_prompt`, `cast` and
`constant_lore` in the prefix; `selected_lore`,
`decisions`, `scene_guidance`, `user_message` and `depth_lore` in the Writer
request. Markers cannot be deleted, renamed, or moved across the divider. Each
can be switched off like a custom block; the setting that drives it still
decides whether it has anything to render, and the group context mode decides
what `cast` carries. A switched-off marker only leaves the prompt: the stage
that produces it still runs, and the Inspector still shows its output.

The engine places its remaining Writer lines itself rather than as blocks. The
group speaker frame opens the Writer request and the "write as" line closes it,
both as user text.
The state block, under `**Current State**`, follows `scene_guidance` and leaves
with it when `scene_guidance` is switched off. The tool notice, the Length Guard
nudge and the group speaker's cue, in that order, lead `user_message`; with
`user_message` switched off they still go, as user text just before the "write
as" line.

The tool notice renders whenever the Writer request carries tool schemas. The
Writer ships the shared schemas under `tool_choice="none"` to keep the cached
prefix, but auto-only routes rewrite that to `auto` and some templates ignore
it; the notice is what keeps the Writer writing prose there. It says "this
turn" because the Editor and later passes replay the request before asking for
their own tool call. The nudge renders only while Length Guard enforces.

The Director stage hands the Writer `decisions` (the Judge's guidance lines for
the Writer), `scene_guidance` (the Director's mood prompts and field values)
and the state block. The Inspector's injection block and the Director's own
requests still see decisions and Scene Guidance under their fixed headings,
`**Major Decisions**` and `**Scene Guidance**`.

Lore reaches the layout the same way: `constant_lore`, `selected_lore` and
`depth_lore` carry entry lines only, authored and Agent-managed entries in one
list, and the Director's own request frames selected lore under its fixed
`**Lorebook**` heading.

`main_prompt` and every marker in `MARKER_TEMPLATES` hold their own text
(`EDITABLE_MARKERS`); the API marks them `editable`. Those are the three lore
markers, `decisions`, `scene_guidance` and `user_message`. Each starts from its
template and must keep its own placeholder, `{{<kind>}}` (`{{constant_lore}}`,
`{{decisions}}` and so on) outside a `{{// ... }}` note and outside backticks.
The placeholder stands for the engine's part and is filled like a section
placeholder: the text's macros resolve, with the conversation's seed above the
divider and fresh below it, then the value goes in literally.
The block renders nothing while its value is empty. Writer-request text carries
its own `___` divider. Every other marker's text is fixed; its window has no text field.

The **Main prompt** marker holds the system prompt text in its `content`. A
character card's own `system_prompt` replaces that text unless
`prevent_prompt_overrides` is set (`prompting.char_context`). Switching the
marker off leaves no main prompt at all, the card's included, for the
summarizer and the size estimate as well. Either text reaches the layout raw:
the layout fills the sections it names and resolves its macros with the
conversation's seed, as prefix custom blocks do, but keeps its whitespace, so
text without macros keeps its bytes. Every lane,
including a separate Agent lane, off-turn calls, and the summarizer read the
active layout's Main prompt.

Every other block is a **custom block**, including the Character, Scenario,
Example dialogue, Chat instructions and User persona blocks a new layout seeds
(`SECTION_BLOCKS` in `core/prompt_layout.py`). A custom block may be edited,
switched off, deleted, or moved to either side of the divider; one moved
across it with its old side's default role (`system` above, `user` below)
takes the new side's default, and a role the user picked stays.

Every layout text may name section placeholders: a custom block's, the Main
prompt (a card's `system_prompt` included) and every editable marker's. Each
name means its section wherever it is written, on either side of the divider:

| Placeholder | Section |
|---|---|
| `{{description}}` | A solo card's description; empty in a group, where `cast` carries the cards |
| `{{personality}}` | A solo card's personality; empty in a group |
| `{{scenario}}` | The chat's scenario |
| `{{examples}}` | A solo card's example dialogue; empty in a group |
| `{{instructions}}` | The chat's instructions; empty under `prevent_prompt_overrides` |
| `{{persona}}` | The user persona's description |

`{{description}}` and `{{personality}}` are also general macros carrying the
same card fields, scoped to the speaker in a group; in layout text they are
sections, so they stay empty in a group. The other placeholders do not resolve
outside layout text.

Only a custom block renders nothing while every section it names is empty; the
Main prompt and the editable markers drop an empty section with its blank line
and otherwise render. A custom block naming `{{examples}}` is filled once per
example (below); in the Main prompt or a marker's text, `{{examples}}` inserts
every example at once, `<START>` lines dropped, a blank line between examples.

Rendering rules:

- Section values arrive macro-resolved from `prompting.conversation_sections`,
  the one builder every prefix, the Writer request and the size estimate read.
  A block's own macros resolve first, then each placeholder is substituted
  literally, with no second macro pass. A placeholder in single backticks stays
  literal, backticks included, and one inside a `{{// ... }}` note leaves with
  the note; neither counts as naming its section (`core.macros.named_placeholders`
  is the one scanner, for rendering and for the marker edit check). An empty
  section's placeholder leaves with one
  blank-line separator beside it, the one before it when there is one, so the
  seeded Character block reads `heading\ndescription\n\npersonality` with
  either field missing cleanly. A block naming placeholders renders nothing
  while all of them are empty, except that a solo Character block keeps its
  heading. A block naming none renders like any custom block.
- A block naming `{{examples}}` is filled once per `<START>` example and keeps
  the blank lines between examples.
- Every block above the divider has a role, and each becomes a message of its
  role in the order laid out, markers included; adjacent messages with the same
  role, including the first history row, merge with a blank line. A `system`
  block after a `user` or `assistant` block stays there rather than joining the
  first system message. The Main prompt opens the prompt even when empty while
  it leads with the `system` role, so the next system block keeps its separator;
  a message left empty is not sent, so a layout without system text sends no
  system message.
- Every block below the divider has a role too, `user` by default, so a layout
  that sets none sends one user message. Each block becomes a message of its
  role after history; adjacent blocks of one role share a message, and a
  message that another follows drops its trailing blank line. Nothing merges
  into the last history row, so the cached prefix keeps its bytes. A custom
  block takes the `___` divider only when it continues a message of its role.
  The turn's attachments ride the message holding the User message when that is
  a user message, else the last user message, else a user message of their own.
  Section values reach the Writer through `PipelineContext.sections`, the same
  values the prefix renders.
- Assistant blocks that end the Writer request are the reply's opening
  (prefill): the last keeps no trailing whitespace, and the engine's closing
  user text (the "write as" line, and the engine lines while `user_message` is
  switched off) goes before them. The later passes replay the reply as the
  continuation of that opening (`prompting.layout.replay_reply`), the bytes the
  Writer produced; the stored reply is the model's text alone.
- The messages go out as laid out, on every transport: a request may end on an
  assistant turn, and a system message may follow history. Orb never moves or
  re-roles a message to fit an API. Where the wire cannot carry the shape, the
  call fails with that reason: the Anthropic Messages API, which takes system
  text only ahead of the turns, refuses a later system message before sending
  (`PromptShapeError`), and a chat template that refuses one answers with its
  own error. The later passes replay the request unchanged, then the reply.
  Anthropic's current models refuse an assistant prefill outright.
- Prefix blocks resolve with the conversation's macro seed, so their bytes are
  stable across builds. Writer-request blocks roll `{{random}}` and `{{roll}}`
  fresh each time the request is built (`Macros.fresh`); a `{{pick}}` keeps the
  seed. The Editor replays the built messages, so both passes read the same
  rolls.

A value rolls once, where it is defined. `Macros.at(place)` narrows the
conversation's seed to one place: a section (`scenario`, `persona`,
`examples`, `instructions`), a layout block (`layout:<kind>` for a marker,
`layout:<id>` for a custom block, so moving one keeps its rolls), a lore entry's
name or content, a group member's field (`prompting.member_place`), a prefill, the
Director's scene guidance and state block, an image-generation field. The seed
inside one place is unchanged: the macro's text and its count among identical
macros there, so editing other text keeps a roll. `Macros.with_card_fields`
resolves a card's description and personality once, at their own places, and
every `{{description}}` inserts those bytes: a section, a lore entry, a history
row and the Judge's Situation read the same rolls. Every builder derives the same
places, so lanes keep identical prefix bytes. `POST
/api/conversations/{id}/reroll-macros` replaces the conversation's `macro_seed`
and clears its stored mood choices, refused while the chat has work running.

No macro pass runs over a built prefix. The first pass is complete:
`{{description}}` and `{{personality}}` each insert a field that already fills
the other field's macro one level deep (that field's own card-field macros
dropped, which cuts cycles). A turn lane resolves identity macros only in each
call's trailing messages (`CachedBase.resolve_trailing`), so every call on a
turn lane, a forced workflow call and the off-turn prefix send the same prefix
bytes, the ones the Inspector and the size estimate show.

Every turn and off-turn builder renders the same active layout:
`load_pipeline_context` reads it once into `PipelineContext.layout`, and
`toolkit.build_offturn_prefix` reads it from `database/`. Every lane of a turn
shares one prefix (`pipeline.context.build_turn_prefix`). The summarizer reads
the same neutral prefix over the summarized history
(`pipeline.context.offturn_config`), so it renders the active layout too.

The HTTP surface lives in `api/routes/prompt_layouts.py`:

| Route | Purpose |
|---|---|
| `GET /api/prompt-layouts` | Layouts and the active id |
| `POST /api/prompt-layouts` | Create a layout; `{copy_from}` duplicates one |
| `PATCH`, `DELETE /api/prompt-layouts/{id}` | Rename or delete (not the last layout); deleting the active layout activates the first one left |
| `PUT /api/prompt-layouts/active` | Set the active layout |
| `GET`, `POST /api/prompt-layouts/{id}/blocks` | List blocks, or add a custom block |
| `PATCH`, `DELETE /api/prompt-blocks/{id}` | Edit a block's label, text, role or switch (a marker only its role, its switch, and its text when editable), or delete a custom block; a refused edit is a 409 |
| `PUT /api/prompt-layouts/{id}/order` | Place every block: `{prefix: [...ids], writer: [...ids]}` |
