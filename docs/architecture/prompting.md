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
the built-in blocks and their templates); `prompting/layout.py` renders it. Chat mode reads the
active layout; Document mode and the Assistant panel do not.

- **Prefix** blocks (above the divider) build the system message and optional
  pre-history messages. Every pass and off-turn workflow call reads them.
- **Writer request** blocks (below the divider) build the Writer's single user
  message. The Editor replays that message; the Director never sees it.
  Continue sends no Writer request, so these blocks do not steer it.

A **marker** stands for a built-in section: `main_prompt`, `character`, `cast`,
`constant_lore`, `scenario`, `example_dialogue`, `card_instructions`,
`user_persona` and `workflow_blocks` in the prefix; `selected_lore`,
`scene_guidance`, `length_nudge`, `cue`, `user_message` and `depth_lore` in the
Writer request. A marker has a toggle only when no other setting already
switches it (`character`, `scenario`, `example_dialogue`, `card_instructions`,
`user_persona`); the group context mode decides what `cast` carries. Only `card_instructions` and custom blocks may cross the
divider. The group speaker frame always opens the Writer request and the
"write as" line always closes it; the tool notice renders directly before the
`length_nudge` marker.

Rendering rules:

- A marker's `content` is its template, seeded from `MARKER_TEMPLATES` (for
  example `## Scenario\n{{content}}`); the backend supplies only the section's
  own text. The template's macros resolve first, then `{{content}}` is
  substituted literally, with no second macro pass. A marker whose section is
  empty renders nothing, whatever its template says, except that a solo
  `character` keeps its heading when the card has no description.
- `example_dialogue` fills its template once per `<START>` example and keeps the
  blank lines between examples. `character` carries a solo card's description;
  `cast` carries the group cast section, which has its own headings.
- Enabled `system` blocks join, in order, into one system message with the
  built-in separators. `user` and `assistant` prefix blocks become messages
  between the system message and history; adjacent messages with the same role,
  including the first history row, merge with a blank line.
- The Writer request is always one user message. Custom blocks there use the
  `___` divider, as lore does, and roles do not apply.
- Prefix blocks resolve with the conversation's macro seed, so their bytes are
  stable across builds. Writer-request blocks roll inline macros fresh each time
  the request is built; the Editor replays the built string, so both passes read
  the same rolls.

Every turn and off-turn builder renders the same active layout:
`load_pipeline_context` reads it once into `PipelineContext.layout`, and
`toolkit.build_offturn_prefix` reads it from `database/`. The summarizer always
uses the built-in order.

The HTTP surface lives in `api/routes/prompt_layouts.py`:

| Route | Purpose |
|---|---|
| `GET /api/prompt-layouts` | Layouts and the active id |
| `POST /api/prompt-layouts` | Create a layout; `{copy_from}` duplicates one |
| `PATCH`, `DELETE /api/prompt-layouts/{id}` | Rename or delete (not the active or last layout) |
| `PUT /api/prompt-layouts/active` | Set the active layout |
| `GET`, `POST /api/prompt-layouts/{id}/blocks` | List blocks, or add a custom block |
| `PATCH`, `DELETE /api/prompt-blocks/{id}` | Edit a block, or delete a custom block |
| `PUT /api/prompt-layouts/{id}/order` | Place every block: `{prefix: [...ids], writer: [...ids]}` |
