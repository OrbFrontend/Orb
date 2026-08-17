# Group character-context modes — implementation plan

## Decision

Add **Character context** as the first control in the Group settings *body*
(the modal opens with Scene title; this becomes the first control of a new
`Character context` section, above `Reply behavior`). It controls which
character information is sent to every group generation; it is separate from
reply behavior, which controls *who* speaks.

Use these three user-facing modes:

| Label | Stored value | One-line description |
|---|---|---|
| **Private perspective** | `private` | Each speaker receives its full card; other members receive only their public profiles. |
| **Shared dossier** | `shared` | Every speaker receives a labelled identity dossier for the whole cast. |
| **Classic card swap** | `swap` | Only the active speaker's card is sent, in the conventional group-chat layout. |

`Private perspective` is the default for every conversation. It is
verified to be the current Orb behaviour (`build_prefix`'s grouped branch emits
`## Cast` + public profiles only; `build_writer_content` appends the speaker's
`private_sheet`/`mes_example`/`post_history` to the writer's trailing user
message), so the default is behaviour-preserving.

The control needs a persistent, mode-specific explanation directly below the
dropdown. In particular, Shared dossier must say that every active member can
read the other members' shared identity details, and Classic card swap must say
that changing speakers can reduce prompt-cache reuse.

## Field inventory (what actually exists in Orb)

The mode only redistributes fields Orb already carries. There is no per-card
depth note and no per-card scenario in any group path — do not invent either.

| Source | Where it lives today | Private | Shared | Swap |
|---|---|---|---|---|
| `description` + `personality` | `CastMember.private_sheet` (`pipeline/cast.py:_private_sheet`) | speaker tail | shared dossier | active-card system block |
| `mes_example` | `CastMember.mes_example` | speaker tail | shared dossier | active-card system block |
| `post_history_instructions` | `CastMember.post_history` | speaker tail | speaker tail (never merged) | speaker tail |
| public profile / scene override | `CastMember.public_profile` (`extensions.orb.public_profile`, or `group_members.public_profile_override`) | shared `## Cast` block | inside that member's dossier (see below) | not sent |
| card `system_prompt` | — | ignored in group mode | ignored | ignored |
| card `scenario` | — | never read by any group path | never read | never read |

Two consequences that must be written into the code, not assumed:

- **Per-card scenario stays out of dossiers.** A group has exactly one scene
  premise (`conversations.character_scenario`). Merging N card scenarios into
  the prefix would create N competing premises. This is a deliberate divergence
  from SillyTavern's Join-cards behaviour.
- **`prevent_prompt_overrides` still applies** to `post_history` in every mode,
  and card `system_prompt` stays ignored in every mode — including Swap, where
  SillyTavern *would* apply it. Swap means identity-field substitution, not
  control-instruction substitution.

## Prompt contracts

All three modes retain the group title, conversation scenario, user persona,
stable lorebook constants, workflow system blocks (`extra_system_blocks`), the
per-turn lorebook/`at_depth` blocks, the speaker beat, the full speaker-labelled
message history, and the current active speaker's final "write only this
speaker" instruction. Member names remain available through `{{cast}}` and
history labels in every mode.

### Private perspective — default

```
system: global prompt + scenario + stable lore + ordered public cast
history: labelled shared group history
user:   "## You are writing as X" + private sheet + examples + active-only
        instructions + lorebook/scene direction + current request + beat
        + @depth block + "write as this speaker only"
```

- The public cast contains only confirmed `public_profile` material.
- A full card's description, personality, examples, and post-history
  instructions are visible only in that card's Writer/Editor path.
- This preserves the existing privacy boundary and has one stable shared trunk
  before each speaker-specific tail.

### Shared dossier

```
system: global prompt + scenario + stable lore
        + ordered, labelled identity dossiers for every active member
history: labelled shared group history
user:   "## You are writing as X" + active-only instructions + scene direction
        + current request + beat + "write as this speaker only"
```

- A dossier carries `description` + `personality` + `mes_example` for that
  member, and — when the member has a `public_profile_override` — that override
  as a labelled `Scene profile:` line inside the same dossier. The override is
  user-authored scene framing; silently dropping it in this mode would be a
  regression against Manage cast's promise.
- **No duplication.** In this mode the active speaker's identity fields move out
  of the writer tail entirely; they are not repeated after history. The tail
  keeps only the `## You are writing as X` header, active-only directives, and
  the closing guard.
- Deliberately do **not** merge per-card system prompts or post-history
  instructions into the dossier. They are directives, not shared facts, and
  concatenating them creates contradictory control instructions. The active
  speaker's such directives remain active-only, in the tail.
- Every dossier must have an unambiguous heading, for example
  `## Character dossier: Aria`; never concatenate raw cards into an anonymous
  blob.
- The dossier set is the **active** roster in canonical `sort_order, id` order —
  muted members included (a muted member is in scene but never speaks),
  tombstoned (`active = 0`) members excluded. The
  `idx_group_member_active_card` unique index guarantees one dossier per card.
- Narrator members and members with no `character_card_id` contribute a heading
  plus their override only, or are skipped entirely when both are empty. Pick
  one and make it deterministic — an empty `## Character dossier: …` heading is
  wasted prefix.
- The whole dossier block is stable while the active roster and its cards are
  stable, so every speaker shares the same cache prefix. Editing any included
  card or changing the active roster invalidates that prefix.

### Classic card swap

```
system: global prompt + scenario + stable lore + names-only cast list
        + active speaker's identity fields
history: labelled shared group history
user:   "## You are writing as X" + active-only instructions + scene direction
        + current request + beat + "write as this speaker only"
```

- Do not include other members' public profiles or identity fields in this
  mode. Their names remain in the names-only cast list, the `{{cast}}` macro,
  and the message labels. Keeping the names-only list (rather than dropping the
  `## Cast` section) is what lets the Director's neutral base and the Writer's
  per-speaker base share every byte up to the active card.
- This is the closest semantic match to SillyTavern's default Swap character
  cards mode: the selected character's identity fields are substituted before
  the shared history. It is *not* a full SillyTavern swap — see the
  `system_prompt` note above.
- It provides raw-card isolation but creates a separate prefix/cache lineage
  for each speaker. Because the swap sits *before* history, a speaker change
  invalidates the history KV too, not just the card region. A local
  single-prefix cache loses most of its history hit when the speaker changes;
  cloud providers that retain multiple prefix branches may keep one warm lineage
  per character.

### Macro resolution (new rule — do not skip)

Today `{{char}}` resolves to the **group title** in every shared field, and only
the private speaker tail resolves it to the speaker (`build_writer_content`
builds a speaker-scoped `Macros`). Shared dossier and Classic card swap move
card text *into* the shared prefix, so without a rule a card that says
"{{char}} never lies" would silently start describing the group.

- Each dossier resolves `{{char}}` to **that member's** name and `{{cast}}` to
  the roster, using a per-member `Macros` — the same scoping the writer tail
  already uses.
- The swapped active card resolves `{{char}}` to the active speaker.
- Everything outside a dossier/active card (title, scenario, style blocks,
  lorebook) keeps group-title scoping.
- `{{random}}`/`{{roll}}` keep the conversation `macro_seed`, so per-member
  resolution stays byte-stable turn over turn.
- Update `docs/features/group-chats.md`, whose current one-line rule ("`{{char}}`
  means the group title in shared fields") becomes mode-dependent.

## Pipeline and KV-cache design

The mode must be resolved once into an immutable per-turn context contract,
then threaded into every pass. No pass may independently decide which card
fields are visible.

| Mode | Director prefix | Writer/Editor prefix | Cache consequence |
|---|---|---|---|
| Private perspective | shared public-cast prefix | same prefix; private sheet only in trailing Writer content | One common historical trunk; the active sheet is fresh after history. |
| Shared dossier | shared-dossier prefix | same prefix | Best prefix sharing across speakers, but every call carries all dossier tokens. |
| Classic card swap | neutral names-only group prefix | names-only prefix + per-speaker active card | One cache lane per speaker; the Director cannot prewarm a selected speaker's Writer prefix. |

The Director runs before the speaking plan exists. In Classic card swap it must
therefore use the neutral group prefix, never an arbitrary member's card. The
Writer gets a frozen speaker-specific `CachedBase` after the plan is known.
The Editor already replays the exact Writer messages (`build_writer_content` is
built once and threaded into both passes) and must keep doing so; it must not
rebuild a different active-card prefix.

Concrete call sites the refactor has to cover — all of these currently assume
one prefix per beat:

- `prompt_builder.build_prefix` hard-suppresses `char_persona`, `mes_example`
  and `post_history` whenever `cast.grouped`. Swap needs those (speaker-scoped)
  back in the system body, so the mode has to reach `build_prefix` itself — the
  cheapest route is a field on `TurnCast`, which is already threaded there.
- `context._build_prefix_from_ctx` / `_build_prefixes` build the writer prefix
  **and** the dual-model `agent_prefix`. Both lanes must be built under the same
  mode, or the Editor's agent lane sees a different cast than the Writer.
- `context._prepare_turn` hands `prefix_base` to the pre-pipeline workflow
  hooks. In Swap that base must be the neutral one; hooks run before the plan
  exists.
- `entrypoints._generate_group_beat` reuses `setup.prefix` verbatim for the
  first planned speaker (`index == 0`) and only rebuilds for later speakers. In
  Swap the first speaker must rebuild too — the shortcut is a correctness bug in
  that mode, not just a cache miss.
- The Dynamic Worlds / direction-note / feedback steps ride the same base;
  they inherit the mode with no extra work, but the KV parity tests should
  assert that rather than assume it.
- `ConversationSummarizer.build_messages` calls `build_prefix` with the cast.
  **Decision: the summarizer always uses the Private/public-cast projection**,
  regardless of mode. Compression is scene-wide narration; paying for every
  dossier (or swapping in one arbitrary card) buys nothing and inflates the one
  call that is already the longest. State this explicitly in the code comment,
  because it is the one place the mode intentionally does not apply.

Maintain the existing byte-stability rules within a mode: canonical roster
order, stable headings, stable tool-schema order, and deterministic macro
resolution. A mode change is intentionally a cache miss on the next request.

### Billing guidance in the settings UI

This is explanatory copy, not a promised token estimate:

- Private perspective: efficient when speakers change on a single-prefix
  server; it avoids repeatedly re-prefilling the long common history.
- Shared dossier: potentially cheapest after warm-up on providers with
  discounted cached input, but its cold prompt is the sum of all dossiers and
  it shares information by design.
- Classic card swap: efficient for a small, stable cast only when the provider
  retains the relevant character's cache branch. It is usually the least
  cache-efficient mode on a one-prefix local server when speakers alternate.

Do not surface dollar amounts: cached-input discounts, cache retention, model
templates, and tool rendering are endpoint-specific.

## Data and API work

1. Add `conversations.group_context_mode TEXT NOT NULL DEFAULT 'private'
   CHECK (group_context_mode IN ('private', 'shared', 'swap'))`. Use a real
   CHECK constraint, matching `kind`, `group_turn_mode` and `group_max_speakers`
   — application-only validation would be the odd one out.
2. **`database/schema.py` only — no migration.** Group chats have never been
   deployed off the development laptop and no group conversation exists, so
   there is no installed database with group data to upgrade. Adding the column
   to the `CREATE TABLE conversations` block is the whole schema change; every
   real install of this feature is a fresh install, which gets the full schema
   and is stamped past the migration chain.
   `tests/integration/test_fresh_install_stamping.py` stays green on its own —
   it builds both sides from `schema.py`, and a chain with nothing to say about
   this column is a no-op on both.
   *One dev-box step, not a migration:* the working database is already stamped
   at `0054_group_chats`, so it will never see a `schema.py` edit. Either
   recreate `backend/data/app.db`, or hand-apply the identical DDL once:
   `ALTER TABLE conversations ADD COLUMN group_context_mode TEXT NOT NULL
   DEFAULT 'private' CHECK (group_context_mode IN ('private', 'shared',
   'swap'));`. Declaring the column last in `schema.py` keeps that hand-applied
   DDL textually aligned with the generated schema.
3. Add the field to `database/models.py:ConversationRow`, `api/schemas.py`
   (`ConversationCreate` with a `Literal[...] = "private"`, `ConversationUpdate`
   with `Literal[...] | None = None`), the `update_conversation` allowlist in
   `database/queries/conversations.py`, `create_group_conversation`, and
   `fork_conversation`'s group branch (checkpoint / compression forks must carry
   the mode, exactly as they carry `turn_mode`/`max_speakers`).
4. **No work needed** for: conversation reads (`get_conversation` is `SELECT *`,
   `list_conversations` is `c.*`), presets/backups (the merge engine derives
   columns from `PRAGMA table_info`; `preset_schema.py` only needs touching for
   new tables or secret-looking columns, and the coverage test says so), and
   `convert-to-group` (it inserts no group settings, so the column default
   applies). Drop these from the work list rather than budgeting for them.
5. Preserve solo behaviour: the field is stored but ignored unless
   `conversations.kind == 'group'`.

## Backend work

1. Introduce a typed context-mode domain value (`GroupContextMode` alongside the
   other `Literal` aliases in `core/domain_types.py`) and a single
   prompt-projection helper. It owns the field allowlist, headings, canonical
   ordering, per-member macro scoping, and the choice between public profile,
   shared dossier, and active-card projection.
2. Carry the resolved mode on `TurnCast` (it already reaches `build_prefix`,
   `build_writer_content` and the summarizer) plus enough pre-rendered data on
   `PipelineContext` to construct the frozen base without further database reads
   in individual passes. `resolve_cast` already loads every member's card, so
   dossier text can be built there.
3. Refactor prefix construction so Private perspective and Shared dossier build
   one common base per model lane, while Classic card swap builds a neutral
   Director base plus one frozen Writer base per selected member — covering the
   `index == 0` shortcut and the `prefix_base` handed to pre-pipeline hooks.
4. Keep speaker private instructions out of public or shared system blocks in
   every mode. The mode changes identity visibility, not the final speaker-only
   generation guard.
5. Update context-size reporting (`api/routes/conversations.py`, the group
   branch around the `cast_public` / `largest_speaker_tail` components):
   - Private perspective: shared public prefix + history + largest private tail
     (unchanged; keep the existing component keys — `test_group_chats.py`
     asserts `largest_speaker_tail`).
   - Shared dossier: all active dossiers + history + largest active-only tail.
   - Classic card swap: largest active full-card prefix + history + active-only
     tail.
   New per-mode component keys need no frontend work: `renderContextSize`
   renders `Object.entries(breakdown)` generically and title-cases the key.
6. Ensure the Dynamic Worlds catalog, card-linked Worlds, fragments, workflows,
   and tool schemas retain their current scope. Context mode must not
   accidentally turn scene-wide lore into per-character private lore.

## Frontend work

1. Add the `Character context` dropdown as the first control of a new
   `Character context` section in Group settings, above the `Reply behavior`
   section (the modal's own first field, Scene title, stays first).
2. Render the three descriptions and a concise cache/privacy disclosure when
   the selected option changes. Do not use a confirmation dialog; the setting
   changes future prompt visibility only and is reversible.
3. **Rewrite `CONTEXT_LINE` and `CONTEXT_HELP` in `frontend/group_setup.js`.**
   They currently hardcode the Private-perspective contract ("A member's raw
   card fields … are sent only on that member's own speaking turn") and are
   rendered in *three* surfaces — create, settings, and convert-to-group. Left
   alone they become an outright false privacy claim under Shared dossier and a
   misleading one under Classic card swap. Make the line mode-aware where a mode
   is known, and mode-neutral in the create/convert modals.
4. Leave the create and convert-to-group flows on the `private` default; do not
   add a fourth Advanced control to the create modal. A new scene has no cast
   history to reason about, and the setting is one click away afterwards.
5. Persist through the existing conversation update path (`PUT
   /api/conversations/{cid}`) and mirror the value onto `S.groupCast` in the
   same place `turn_mode`/`max_speakers` are mirrored. Keep the option
   unavailable while no group is active, as with the rest of Group settings.
6. Make the Manage cast override box mode-aware. Its one line of placeholder
   copy ("Public profile override — how the rest of the cast sees them") is a
   *Private perspective* sentence. Under Shared dossier the same text rides
   inside that member's dossier; under Classic card swap it is not sent at all.
   For v1: relabel per mode, and in Swap disable the box with a one-line reason
   rather than silently accepting text that never ships. Do **not** repoint the
   box at a different storage slot per mode — one string, one meaning (see
   "Generated projections" below).
7. Keep terminology distinct, and match the existing US spelling used in the UI
   and `group_cast.js:TURN_MODES`:
   - **Character context**: what each speaker is told.
   - **Reply behavior**: who replies and in what order.
   - **Manage cast**: roster, mute state, public profiles, and member order.

## Generated projections (forward-looking, not v1)

Orb already generates one projection: `POST /api/characters/{card_id}/public-profile/generate`
drafts `{appearance, role}` from the card's private fields under a redaction
prompt ("no secrets, private instructions, example dialogue, or internal
motivations"), returns it as an editable draft, and never writes the card. The
user reviews it in the card editor's Advanced tab and saves. Per scene,
`group_members.public_profile_override` replaces the whole rendered profile with
free text.

Modes turn that one projection into a family, and the generator objective is
**different per mode** — this is the part that is easy to get wrong:

| Mode | Projection | What a generator is for |
|---|---|---|
| Private perspective | public profile | **Redaction.** Decide what other characters may know. Today's generator, unchanged. |
| Shared dossier | identity dossier | **Compression.** Nothing is being hidden — every speaker sees it by definition. The cost is that the full dossier is billed on every call by every speaker, so the useful draft is a tight digest of description + personality + examples, not a sanitised one. |
| Classic card swap | — | No cross-member text exists. No generator surface; the roster box is inert. |

Design rules for whenever that lands:

- **Generate on demand, store, reuse byte-for-byte.** Generated text that
  reaches the shared prefix must be persisted and served unchanged. A generator
  invoked lazily inside a turn would rewrite the prefix mid-conversation and
  bust every cache lane this plan is built around. Keep the existing
  draft → review → save contract; never auto-write.
- **One slot per meaning, not one slot per mode.** A generated dossier digest
  wants the same two-tier shape the public profile has (card-level
  `extensions.orb.*` for reuse across scenes, plus a per-scene override column),
  but it must be its *own* slot. Feeding `public_profile_override` into the
  dossier would take a deliberately redacted one-liner and promote it to the
  character's entire identity the moment the user flips the mode.
- **Track staleness.** Nothing today records which card revision a generated
  profile came from. Store the source card's `updated_at` (or a hash) beside the
  generated text so the UI can say "the card changed since this draft" instead
  of shipping quietly stale identity into the prefix.
- **Batch sequentially, per member.** A "generate for the whole cast" action in
  Manage cast is one agent-lane call per member, mirroring the existing
  single-card schema. Sequential with per-row status keeps schemas small and
  isolates a failure to one row.

## Tests and verification

1. Fresh-install tests: default is `private`; solo conversations are unaffected;
   `test_fresh_install_stamping` stays green. There is no migration to test.
2. API validation tests: accept only the three stored values; reject an unknown
   value without partial mutation. Cover the fork paths (checkpoint, compression)
   carrying the mode.
3. Prompt snapshot tests for every mode and at least two members:
   - Private perspective never exposes B's raw card in A's Writer or Editor
     prompt, while B's public profile remains visible.
   - Shared dossier contains every labelled identity dossier exactly once, keeps
     active-only directives private, and does **not** repeat the active
     speaker's identity fields in the tail.
   - Classic card swap contains only the selected member's raw identity fields
     before history, plus the names-only cast list and no other member profile.
   - `{{char}}` inside a member's card resolves to that member in its dossier /
     swapped card, and the group title elsewhere.
4. KV parity tests:
   - Private and Shared have byte-identical pre-history Writer prefixes for A
     and B, in both the writer lane and the dual-model agent lane.
   - Classic card swap differs only from the active-card portion onward and uses
     a neutral Director base — including for the first planned speaker.
   - Editor replays the exact Writer input in all modes.
5. Context-size tests cover all three formulas and roster changes, muted
   members, tombstoned historical speakers, narrator/cardless members, empty
   profiles, and cards without examples or directives.
6. Summarizer test: compression prompts stay on the public-cast projection in
   all three modes.
7. Run `./scripts/lint.sh`, `tests/unit/test_group_prompt.py` +
   `tests/integration/test_group_chats.py`, then `./scripts/tests.sh all`
   before release.

## Documentation and rollout

- Update `docs/features/group-chats.md`: replace the single privacy-boundary
  description with the mode-specific rules, and make the `{{char}}` scoping rule
  mode-dependent.
- Update `docs/architecture/kv-cache.md` with the three cache topologies,
  including the fact that Classic card swap prevents Director-to-Writer
  prewarming and invalidates the history KV, not just the card region.
- No migration release note is needed: group chats ship with this control
  already present, so there is no installed group whose privacy boundary
  changes. Document Private perspective as the default in `group-chats.md`
  instead.

## Out of scope, but found while auditing this plan

`conversations.post_history_instructions` — the "Style & behavior instructions"
field that Group settings saves, one control below where this dropdown lands —
never reaches a group prompt. `context._build_prefix_from_ctx` passes it to
`build_prefix`, which drops it under `if resolved["post_history"] and not (cast
and cast.grouped)`, and the group writer tail only ever uses the *card's*
`post_history`. Group members' style instructions are therefore write-only
today. Fix it separately (it is a one-line gate change plus a test), and decide
its placement before this feature ships — it is scene-wide direction, so it
belongs in the shared system body in all three modes.
