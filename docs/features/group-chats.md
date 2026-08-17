# Group chats

Group chats are durable cast conversations. `conversations.kind = 'group'`
switches prompt construction and turn execution from the legacy scalar card to
the ordered `group_members` roster. Assistant messages store a member identity
(`speaker_member_id`) and request identity (`beat_id`); the card is resolved
through the member only when current card data, an avatar, or a workflow profile
is needed.

## Character context modes

`conversations.group_context_mode` decides which character information every
generation in the scene carries. It is separate from reply behaviour, which
decides *who* speaks. The UI calls it **Character context** and lives in Group
settings; the stored values are internal. `group_cast.js:CONTEXT_MODES` owns the
per-mode wording — label, one-line hint, description and cache/billing note —
and is the only place that differs per mode. `group_setup.js` owns the two
mode-independent strings beside it: `CONTEXT_COMMON` (the floor all three modes
share) and `OVERRIDE_COPY` (how Manage cast labels the override box per mode).

| Stored | UI label | What the shared cached body carries | What the speaker's trailing message carries |
|---|---|---|---|
| `private` (default) | Private perspective | Every member's confirmed public profile | That speaker's `description`/`personality`, examples, and post-history instructions |
| `shared` | Shared dossier | A labelled dossier per member: `description`/`personality`, that member's curated profile, and examples | That speaker's post-history instructions only |
| `swap` | Classic card swap | A names-only cast list plus the active speaker's `description`/`personality` and examples | That speaker's post-history instructions only |

`backend/inference/group_context.py` is the single owner of that table. Every
consumer — `build_prefix`, `build_writer_content`, and the context-size
estimator — reads the projection from there, so no pass decides card visibility
on its own.

Three rules hold in **every** mode:

- A card's `system_prompt` override is ignored and its `scenario` is never read.
  A group has exactly one premise (`conversations.character_scenario`); merging
  N card scenarios would create N competing ones. Swap is therefore
  identity-field substitution, not control-instruction substitution — a
  deliberate divergence from SillyTavern.
- `post_history_instructions` stays active-only, in the speaker's own trailing
  message, and still honours `prevent_prompt_overrides`. Concatenating several
  members' directives produces contradictory control instructions.
- Member names stay available through `{{cast}}`, the cast section, and the
  speaker labels on history. Card-linked Worlds and card-embedded fragments are
  scene-wide in all three modes, so no mode provides private per-character lore.

Only `private` is a privacy boundary. Under `shared` every active member reads
every other member's card details by design, and under `swap` no cross-member
identity text exists at all.

The dossier/cast set is the **active** roster in canonical `sort_order, id`
order — muted members included (a muted member is in scene but never speaks),
tombstoned members excluded. A narrator or cardless member with nothing to say
about itself contributes no dossier; its name still rides the cast list.

A dossier carries the member's curated profile as well as its card text, so
switching to `shared` never *loses* authored framing — it would otherwise erase
a member whose card is all public profile and no description. The line is
labelled by provenance: `Scene profile:` for a Manage-cast override, `Public
profile:` for the card's own `extensions.orb.public_profile`.

Compression is the one place the mode deliberately does not apply: summary
prompts always use the public-cast projection, whatever the scene is set to.
Scene-wide narration gains nothing from every dossier, or from one arbitrary
swapped-in card, on the longest call in the app.

`{{cast}}` always means the roster names. `{{char}}` is **mode-dependent**: it
means the group title everywhere outside a member's own card text, and that
member's name inside a dossier (`shared`), a swapped active card (`swap`), or a
private speaker tail (`private`). Without that scoping a card reading
"{{char}} never lies" would silently start describing the scene title. Group
chats use the conversation persona pin or the global persona; card persona locks
and card system-prompt overrides do not compete.

## Turn policy and message tree

- `manual`: requires an active, unmuted pinned member.
- `round_robin`: selects one next eligible member.
- `director`: validates a bounded `speaking_plan`; explicit `[]` rests the
  scene, while a missing or malformed plan falls back to round-robin.
- A user pin overrides the plan and selects exactly that member.

The stored mode names are internal. The UI calls them `Auto — Director
chooses`, `Rotate — Cast replies in order` and `Choose — Select every reply`
(`group_cast.js:TURN_MODES` is the only place that wording lives).

A pin is a *temporary override*, not a mode: outside `manual` the client clears
it once the beat it named has produced a reply, so the configured strategy
resumes by itself rather than being silently suppressed. In `manual` the pick is
the strategy, so it survives until it is used or cleared. An aborted or failed
beat keeps the override to retry with.

The Director and pre-pipeline setup run once. Each planned speaker then runs the
Writer, Editor, feedback, and post-workflow path with the shared Director state.
Rows form `user -> speaker 1 -> speaker 2 -> …`, share one beat ID, and receive
incrementing turn indices. Post-turn notes and Dynamic Worlds run only after the
last successful speaker. Keyword/agentic lore selection is frozen for the beat;
later speakers see earlier prose in history, but that prose activates new lore
only on the next beat.

Regenerating a group reply creates a same-speaker sibling under the original
parent. It does not replay downstream speakers. Fork-edit instead creates a new
user sibling and runs a fresh group beat. Removed roster members are tombstoned
so old messages keep their names; re-adding the same card creates a new member
identity.

## HTTP and SSE

Roster creation, conversion, and synchronization are transactional. The main
group routes are:

- `GET|PUT /api/conversations/{cid}/members`
- `POST /api/conversations/{cid}/convert-to-group`
- `POST /api/conversations/{cid}/speak`
- `POST /api/conversations/{cid}/activate`
- `PUT /api/characters/{card_id}/public-profile`
- `POST /api/characters/{card_id}/public-profile/generate`

Every group request emits one `speaking_plan`, then a
`speaker_start`/`speaker_done` pair per persisted reply, and exactly one
request-level `done`. The frontend creates and finalizes a bubble per speaker,
stops the previous audio channel at every `speaker_start`, and performs a full
message refetch/render after the beat.

## Chat surface

The group screen shows four things: scene identity, cast, conversation and
composer. Everything else is contextual or lives in Group settings (`•••` in the
chat header), which owns the durable configuration — title, character context,
reply behavior, max replies per turn, scene premise, style instructions —
through `PUT /api/conversations/{cid}`. Cast membership, order, reply
eligibility and public-profile overrides are edited in Manage cast
(`PUT …/members`). The override box is one string with one meaning in every
mode; only its label changes, and under Classic card swap it is disabled with a
one-line reason rather than accepting text that would never ship.

Creation and convert-to-group do not offer the context control: a new scene has
no cast history to reason about, so both start on Private perspective and the
setting is one click away afterwards.

The speaking-plan rail is painted only while a beat with two or more speakers is
planned or streaming; a single speaker is announced by its cast chip, and a rest
(`[]`) is reported as a toast. `Convert to group` is a solo-conversation action
and never renders inside a group.

Checkpoint and compression copy the full active/historical roster with new
member UUIDs and remap copied message identities. Compression summaries have no
speaker member and render as `Summary`; the summary prompt itself receives
speaker-labelled history. Both forks carry `group_context_mode`, exactly as they
carry the turn mode and speaker cap.

Context-size reporting is the maximum group call, never a sum, and its component
keys follow the mode: `cast_public` + `largest_speaker_tail` under Private,
`cast_dossiers` + `largest_speaker_tail` under Shared, and `cast_names` +
`largest_active_card` + `largest_speaker_tail` under Swap. `renderContextSize`
title-cases whatever keys it is handed, so the split needs no frontend work.

The two `largest_*` components measure only members that can actually take the
turn: the shared body covers the whole roster, but a muted member is never
scheduled, so counting its card would overstate the call. An all-muted scene
generates nothing and measures zero there.
