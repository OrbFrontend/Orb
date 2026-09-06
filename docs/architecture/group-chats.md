# Group Chats

Group chats are durable conversations with a cast. When
`conversations.kind = 'group'`, the ordered `group_members` roster replaces the
single character card used by a solo chat.

Each assistant message records its `speaker_member_id`. The current card is
resolved through that member only when a card, avatar, or workflow profile is
needed.

The user-facing feature is [Group Chats](../features/group-chats.md).

## The vocabulary

These terms describe different things:

| Term | Meaning | Stored or used as |
|---|---|---|
| **Exchange** | One group request and all of its replies | `messages.exchange_id` |
| **Round** | The user's message and every reply since it | Used by history and image prompts |
| **Cue** | One speaker's instruction in a Director plan | `speaking_plan` and `speaker_start` |

Under **Manual**, one round can contain several exchanges because each cast
selection is its own request.

## Group families

A group is a family of conversations, not a single row. The root conversation
has a null `group_root_id`; forks point directly to that root. Checkpoints,
compression, and fork-edit stay in the same flat family.

Rosters are copied at fork time with new member ids. This makes each conversation
a cast snapshot: changing one conversation does not change its siblings.

If the root is deleted, the oldest remaining conversation becomes the root and
the family is relinked before deletion. `DELETE …/group` removes the whole
family. Solo conversations have no family; converting one creates a family of
one.

`group_root_of()` in the backend and `groupRootId()` in `group_cast.js` are the
shared helpers for resolving a family's root.

## Character context

`group_context_mode` controls what card information each generation receives.
It is separate from reply behavior, which controls who speaks. The UI labels
the setting **Character context**.

| Mode | Shared cached body | Active speaker's trailing message |
|---|---|---|
| `private` (default) | Every member's public profile | That member's description, personality, examples, and post-history instructions |
| `shared` | A dossier for every member, including card text and examples | That member's post-history instructions |
| `swap` | Every member's public profile plus the active member's card text and examples | That member's post-history instructions |

`backend/prompting/group_context.py` owns this projection. Prompt construction
and context-size reporting use it rather than deciding card visibility locally.

The following rules apply in every mode:

- A group has one premise, `character_scenario`. Card `system_prompt` overrides
  and card `scenario` fields are ignored.
- A card's post-history instructions belong only to the active speaker. The
  scene's own post-history instructions are shared by every speaker.
- `{{cast}}` is always the roster. `{{char}}` is the group title outside member
  card text and the member's name inside that member's context.
- Card-linked Worlds and card fragments are scene-wide. Context mode does not
  make lore private.

Private and Swap protect the same information: a member's full card is visible
only to that member. They differ in placement. Private puts it after history;
Swap puts it before history so it can cache per speaker. Shared deliberately
opens the cards to the whole cast.

The roster is ordered by `sort_order, id`. Muted members remain in the cast but
cannot speak; removed members are tombstoned so old messages keep their names.

## Turn flow

Reply behavior has three modes:

- `manual` answers the pinned active member. With no pin, the scene rests and no
  model call is made.
- `round_robin` chooses the next eligible member.
- `director` asks the Director for a bounded `speaking_plan`. `[]` means rest;
  a missing or invalid plan falls back to round-robin.

A user pin overrides the plan and selects one member. Outside Manual it is a
one-exchange override; in Manual it remains until used or cleared. Regenerate,
super-regenerate, and Magic Rewrite follow the speaker already recorded on the
message and do not consume a pin.

The Director and pre-pipeline setup run once per exchange. Each planned speaker
then runs the Writer, Editor, feedback, and post-workflow path. Messages form

```text
user → speaker 1 → speaker 2 → …
```

All replies share the exchange id and receive increasing turn indices. Later
speakers see earlier replies in history, but lore selection is frozen for the
exchange. Post-turn notes and Dynamic Worlds run after the last successful
speaker.

A group regenerate creates a same-speaker sibling and does not replay later
speakers. Fork-edit creates a new user branch and starts a fresh exchange.

## Scene-local sheets

`group_members` has two scene-local overrides:

- `public_profile_override`: what the rest of the cast sees;
- `card_sheet_override`: what the member reads about itself, replacing the
  card's description and personality.

They never modify the reusable character card. A stored empty string is a real
override, not a fallback.

Optional sheet updates are proposed after an exchange. The feature is off by
default and is offered under **Private perspective**, where updating a sheet
does not rebuild the shared prefix. The pass makes one call per member that
spoke, using the round as evidence. It stages a proposal; the user applies it
from Manage cast.

`member_sheet_proposals` uses `pending`, `applied`, `rejected`, and `stale`
states. Applying compares the proposal's `base_sheet` with the current sheet
inside a transaction and returns `409` on a conflict. There is at most one
pending proposal per member, and a new proposal replaces the old one. Turning
the feature off stops new proposals but does not hide existing ones.

## Feature scope

When a feature needs a member, the feature uses the speaker recorded on the
message or an explicit member selected by the user. It does not infer a member
from unrelated conversation state.

| Scope | Examples |
|---|---|
| Scene | Worlds, persona, macros, compression, checkpoints, fragments, context size |
| Exchange | Director, agentic lore selection, direction notes, Dynamic World proposals |
| Speaker | Editor, feedback, regenerate, image generation, TTS, character expressions |

Off-turn calls use the same scene prefix as the turn: speaker-labelled history,
the cast section, and the same `resolve_cast` helper. Image prompts use the
round up to the message being rendered, with the active speaker as the primary
subject.

## HTTP and SSE

The main group routes are:

```text
GET|PUT /api/conversations/{cid}/members
POST    /api/conversations/{cid}/convert-to-group
POST    /api/conversations/{cid}/group-conversation
DELETE  /api/conversations/{cid}/group
POST    /api/conversations/{cid}/speak
POST    /api/conversations/{cid}/activate
POST    /api/conversations/{cid}/members/scene-profile/generate
```

Every group request emits one `speaking_plan`, a `speaker_start` and
`speaker_done` pair for each persisted reply, and one request-level `done`.
The frontend creates a bubble per speaker and refetches the exchange after it
closes.

## Chat surface

The screen has the scene, conversation, cast rail, and composer. Scene setup is
one modal with two tabs:

- **Group settings** owns title, context mode, reply behavior, speaker limit,
  premise, style instructions, and group deletion.
- **Cast** owns members, order, mute state, scene-local overrides, and sheet
  proposals.

One Save writes both tabs. The cast rail is also the reply control: selecting a
member speaks immediately in a resting scene, or queues that member while an
exchange is busy. In Manual mode, Send stays disabled until a member is picked.

The sidebar shows one row per group family. New conversation, Conversations,
checkpoints, and compression all preserve the family and its roster snapshot.
