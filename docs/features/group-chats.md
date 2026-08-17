# Group chats

Group chats are durable cast conversations. `conversations.kind = 'group'`
switches prompt construction and turn execution from the legacy scalar card to
the ordered `group_members` roster. Assistant messages store a member identity
(`speaker_member_id`) and request identity (`beat_id`); the card is resolved
through the member only when current card data, an avatar, or a workflow profile
is needed.

## Privacy boundary

The shared cached prompt contains the group title, scenario, user persona, and
each active member's confirmed public profile. It never contains another
member's raw `description`, `personality`, examples, or post-history
instructions. Those fields are appended only to that member's Writer request.
Card-linked Worlds and card-embedded fragments are scene-wide in v1, so this is
raw-card-field isolation rather than private per-character lore.

`{{char}}` means the group title in shared fields and `{{cast}}` means the
roster names. A private speaker tail resolves `{{char}}` to that speaker.
Group chats use the conversation persona pin or the global persona; card persona
locks and card system-prompt overrides do not compete.

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
chat header), which owns the durable configuration — title, reply behavior, max
replies per turn, scene premise, style instructions — through
`PUT /api/conversations/{cid}`. Cast membership, order, reply eligibility and
public-profile overrides are edited in Manage cast (`PUT …/members`).

The speaking-plan rail is painted only while a beat with two or more speakers is
planned or streaming; a single speaker is announced by its cast chip, and a rest
(`[]`) is reported as a toast. `Convert to group` is a solo-conversation action
and never renders inside a group.

Checkpoint and compression copy the full active/historical roster with new
member UUIDs and remap copied message identities. Compression summaries have no
speaker member and render as `Summary`; the summary prompt itself receives
speaker-labelled history. Context-size reporting is the maximum group call:
shared cast/history plus the largest active private speaker tail, not a sum of
all private sheets.
