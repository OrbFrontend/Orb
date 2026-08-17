# Group chats

Group chats are durable cast conversations. `conversations.kind = 'group'`
switches prompt construction and turn execution from the legacy scalar card to
the ordered `group_members` roster. Assistant messages store a member identity
(`speaker_member_id`) and request identity (`beat_id`); the card is resolved
through the member only when current card data, an avatar, or a workflow profile
is needed.

## Group families

A group is one *family* of conversations, not one conversation.
`conversations.group_root_id` names the conversation a family descends from;
NULL means the row **is** that root, so a plain group needs no write there and
only forks carry a value. `group_root_of()` (backend) and `groupRootId()`
(`group_cast.js`) are the only places that fallback is expressed.

Checkpoint and Compress History both fork a group, so without lineage each one
produced a second entry under **Groups** and the roster looked like a property of
the conversation rather than of the group. Every fork now joins the source's
family instead, and the family is **flat**: a checkpoint of a checkpoint points
at the root, never at its parent, so the grouping stays a single key rather than
a chain to walk. Rosters are still per-conversation and are copied at fork time
with new member UUIDs — a checkpoint is a snapshot of the cast as well as of the
history, and editing one scene's cast never reaches back into its siblings.

Deleting a conversation keeps its family together: `delete_conversation`
promotes the oldest survivor to root and re-points the others first, because the
FK's `ON DELETE SET NULL` would otherwise scatter the forks back into one sidebar
entry each. `DELETE …/group` is the other direction — the whole family at once,
which is what the sidebar's × means now that one row stands for the group.
Unlike a character card, which outlives its chats as a reusable asset, a group
has no existence apart from its conversations.

Solo conversations never carry a family; a solo fork leaves the column NULL, and
converting a solo chat to a group founds a family of one.

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
beat keeps the override to retry with. Only the pin the finished beat actually
ran on is cleared — the client latches it at request time (`consumedSpeakerId`),
so a chip clicked *while* the beat streams queues that member for the next turn
and survives the cleanup.

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
- `POST /api/conversations/{cid}/group-conversation`
- `DELETE /api/conversations/{cid}/group`
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

The group screen shows four things: scene identity, conversation, cast and
composer. Everything else is contextual or lives in Group settings (`•••` in the
chat header), which owns the durable configuration — title, character context,
reply behavior, max replies per turn, scene premise, style instructions —
through `PUT /api/conversations/{cid}`. Cast membership, order, reply
eligibility and public-profile overrides are edited in Manage cast
(`PUT …/members`). The override box is one string with one meaning in every
mode; only its label changes, and under Classic card swap it is disabled with a
one-line reason rather than accepting text that would never ship.

The cast rail sits on top of the composer and is the only reply control there is
— there is no separate strategy line. One click on a chip does one of two
things, and the scene decides which (`group_cast.js:castClickSpeaksNow` is the
only definition):

- **Resting scene** — nothing streaming, nothing drafted or attached: the member
  takes the floor immediately via `POST …/speak`. There is no toggle here; the
  click resolves the pick by using it, and the one-shot cleanup drops the pin
  afterwards outside `Choose`.
- **Busy or drafted**: the click only queues that member as the next speaker.
  Clicking whoever is already queued takes the pick back.

Muted members render disabled. In `Choose` mode the send button stays disabled
until a member is picked and says so in its tooltip, since nothing else on
screen explains the block.

Creation and convert-to-group do not offer the context control: a new scene has
no cast history to reason about, so both start on Private perspective and the
setting is one click away afterwards.

The speaking-plan rail is painted only while a beat with two or more speakers is
planned or streaming; a single speaker is announced by its cast chip, and a rest
(`[]`) is reported as a toast. `Convert to group` is a solo-conversation action
and never renders inside a group.

The sidebar paints **one row per group**, not per conversation. The row takes its
name from the family's root — a checkpoint renaming itself must not rename the
group — while the avatars, cast line and the conversation a click opens all come
from the family's most recently active member, whose roster is the one currently
in play. A count appears only once a group has branched, since every group starts
at one. The row highlights for any conversation in the family, so a checkpoint
still reads as "this group".

The composer's `•••` is conversation-scoped and asks the *scene* what it is:
`New conversation` starts an empty scene with the same cast in the same family
(`POST …/group-conversation`), and `Conversations` lists the family rather than a
character's chats. Both fall back to the character scope in a solo chat; a group
has no `activeCharId` to key on.

Checkpoint and compression copy the full active/historical roster with new
member UUIDs, remap copied message identities, and keep the copy in the source's
group family. Compression summaries have no
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
