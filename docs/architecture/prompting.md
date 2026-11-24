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
tail rather than the shared prefix, so it may pass `seed=""` and roll
`{{random}}` fresh each call without affecting prefix reuse.

## Prompt layout

A prompt layout is an ordered list of blocks around a fixed **Chat history**
divider. `core/prompt_layout.py` holds the vocabulary (block shape, marker ids,
section placeholders, and the blocks a new layout seeds); `prompting/layout.py`
renders it. Chat mode reads the active layout; Document mode and the Assistant
panel do not.

- **Prefix** blocks (above the divider) build the system message and optional
  pre-history messages. Every pass and off-turn workflow call reads them.
- **Writer request** blocks (below the divider) build the Writer's single user
  message. The Editor replays that message; the Director never sees it.
  Continue sends no Writer request, so these blocks do not steer it.

A **marker** stands for an engine block: `main_prompt`, `cast`,
`constant_lore` and `workflow_blocks` in the prefix; `selected_lore`,
`decisions`, `scene_guidance`, `user_message` and `depth_lore` in the Writer
request. Markers cannot be switched off, deleted, or moved across the divider;
the setting that drives each one decides whether it renders, and the group
context mode decides what `cast` carries.

The engine places its remaining Writer lines itself rather than as blocks. The
group speaker frame opens the Writer request and the "write as" line closes it.
The state block, under `**Current State**`, follows `scene_guidance`. The tool
notice, the Length Guard nudge and the group speaker's cue, in that order, lead
`user_message`.

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
`{{decisions}}` and so on), which stands for the engine's part and is filled
like a section placeholder: the text's macros resolve, with the conversation's
seed above the divider and fresh below it, then the value goes in literally.
The block renders nothing while its value is empty. Writer-request text carries
its own `___` divider. Every other marker's text is fixed; the API's
`builtin_text` field shows it, with its dynamic part in brackets.

The **Main prompt** marker holds the system prompt text in its `content`. A
character card's own `system_prompt` replaces that text unless
`prevent_prompt_overrides` is set (`prompting.char_context`). Either text
resolves macros with the conversation's seed, as prefix custom blocks do, but
keeps its whitespace, so text without macros keeps its bytes. Every lane,
including a separate Agent lane, off-turn calls, and the summarizer read the
active layout's Main prompt.

Every other block is a **custom block**, including the Character, Scenario,
Example dialogue, Chat instructions and User persona blocks a new layout seeds
(`SECTION_BLOCKS` in `core/prompt_layout.py`). A custom block may be edited,
switched off, deleted, or moved to either side of the divider. Its text may
name section placeholders:

| Placeholder | Section |
|---|---|
| `{{description}}` | A solo card's description; empty in a group, where `cast` carries the cards |
| `{{personality}}` | A solo card's personality; empty in a group |
| `{{scenario}}` | The chat's scenario |
| `{{examples}}` | A solo card's example dialogue; empty in a group |
| `{{instructions}}` | The chat's instructions; empty under `prevent_prompt_overrides` |
| `{{persona}}` | The user persona's description |

`{{description}}` and `{{personality}}` are also general macros carrying the
same card fields; inside layout blocks they are sections, so they stay empty in
a group. The other placeholders do not resolve outside layout blocks.

Rendering rules:

- Section values arrive macro-resolved (`prompting.section_inputs`). A block's
  own macros resolve first, then each placeholder is substituted literally, with
  no second macro pass. An empty section's placeholder leaves with one
  blank-line separator beside it, the one before it when there is one, so the
  seeded Character block reads `heading\ndescription\n\npersonality` with
  either field missing cleanly. A block naming placeholders renders nothing
  while all of them are empty, except that a solo Character block keeps its
  heading. A block naming none renders like any custom block.
- A block naming `{{examples}}` is filled once per `<START>` example and keeps
  the blank lines between examples.
- Every block above the divider has a role. Enabled `system` blocks join, in
  order, into one system message with the built-in separators; the Main prompt
  opens it even when empty while its role is `system`. `user` and `assistant`
  blocks, markers included, become messages between the system message and
  history; adjacent messages with the same role, including the first history
  row, merge with a blank line.
- The Writer request is always one user message, so roles do not apply below
  the divider. Custom blocks there use the `___` divider, as lore does. Section
  values reach the Writer through `TurnSetup.sections`, built from the same
  inputs as the prefix.
- Prefix blocks resolve with the conversation's macro seed, so their bytes are
  stable across builds. Writer-request blocks roll inline macros fresh each time
  the request is built; the Editor replays the built string, so both passes read
  the same rolls.

Every turn and off-turn builder renders the same active layout:
`load_pipeline_context` reads it once into `PipelineContext.layout`, and
`toolkit.build_offturn_prefix` reads it from `database/`. Every lane of a turn
shares one prefix (`pipeline.context.build_turn_prefix`). The summarizer uses
the built-in order with the active layout's Main prompt text.

The HTTP surface lives in `api/routes/prompt_layouts.py`:

| Route | Purpose |
|---|---|
| `GET /api/prompt-layouts` | Layouts and the active id |
| `POST /api/prompt-layouts` | Create a layout; `{copy_from}` duplicates one |
| `PATCH`, `DELETE /api/prompt-layouts/{id}` | Rename or delete (not the active or last layout) |
| `PUT /api/prompt-layouts/active` | Set the active layout |
| `GET`, `POST /api/prompt-layouts/{id}/blocks` | List blocks, or add a custom block |
| `PATCH`, `DELETE /api/prompt-blocks/{id}` | Edit a block (custom blocks fully; markers above the divider take a role, the Main prompt its text), or delete a custom block |
| `PUT /api/prompt-layouts/{id}/order` | Place every block: `{prefix: [...ids], writer: [...ids]}` |
