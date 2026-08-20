# Group chats

Group chats are durable cast conversations. `conversations.kind = 'group'`
switches prompt construction and turn execution from the legacy scalar card to
the ordered `group_members` roster. Assistant messages store a member identity
(`speaker_member_id`) and request identity (`exchange_id`); the card is resolved
through the member only when current card data, an avatar, or a workflow profile
is needed.

## Three units, three words

Group chats measure time three ways, and the words are not interchangeable:

- An **exchange** is one group request — `messages.exchange_id`. Every reply a
  single `/send`, `/speak` or regenerate produced shares it. It is what the
  Director plans once for, and what the sheet-update pass is billed per.
- A **round** is the user's last message and every reply since
  (`entrypoints._round_prefix`, `subjects._round_speakers`). Under **Manual** —
  and for any cast-chip click on a resting scene — one round is several
  exchanges, because each click is its own request answering the same message.
- A **beat** is the Director's one-line note for a *single speaker* inside the
  plan (`pipeline/cast.py`), in the screenwriting sense. It is not a unit of
  time at all.

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
| `shared` | Shared dossier | A labelled dossier per member: `description`/`personality` and examples | That speaker's post-history instructions only |
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
- A *card's* `post_history_instructions` stays active-only, in the speaker's own
  trailing message, and still honours `prevent_prompt_overrides`. Concatenating
  several members' directives produces contradictory control instructions. The
  *scene's* own directive (`conversations.post_history_instructions`, the
  "How should this scene be written?" box in Group settings) has no such
  conflict — there is exactly one and it is the same for every speaker — so it
  rides the shared cached body, as it does in a solo chat.
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

A dossier is card text and nothing else. Under `shared` every member already
reads every other member's card, so a curated profile layered on top would be a
second view of the same member — and it rendered as a label on labels
(`Public profile: Appearance: …`). The accepted consequence: a member with no
card text contributes no dossier and rides the cast list as a name, the same
floor a bare narrator already gets.

The public-profile override is therefore a **Private perspective** field. That
is the scene's only privacy boundary, and so the only mode where a curated view
of a member does any work; Manage cast disables the box, and both Draft buttons,
under the other two. One reader still sees it in every mode: compression forces
`context_mode="private"` (`api/routes/conversations.py`), so an override typed
before a mode switch still reaches the *summary* prompt. The disabled copy talks
about the turn for that reason — the string is not sent on a turn, rather than
never read.

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
chooses`, `Rotate — Cast replies in order` and `Manual — Select every reply`
(`group_cast.js:TURN_MODES` is the only place that wording lives).

A pin is a *temporary override*, not a mode: outside `manual` the client clears
it once the exchange it named has produced a reply, so the configured strategy
resumes by itself rather than being silently suppressed. In `manual` the pick is
the strategy, so it survives until it is used or cleared. An aborted or failed
exchange keeps the override to retry with. Only the pin the finished exchange actually
ran on is cleared — the client latches it at request time (`consumedSpeakerId`),
so a chip clicked *while* the exchange streams queues that member for the next turn
and survives the cleanup.

The Director and pre-pipeline setup run once. Each planned speaker then runs the
Writer, Editor, feedback, and post-workflow path with the shared Director state.
Rows form `user -> speaker 1 -> speaker 2 -> …`, share one exchange ID, and receive
incrementing turn indices. Post-turn notes and Dynamic Worlds run only after the
last successful speaker. Keyword/agentic lore selection is frozen for the exchange;
later speakers see earlier prose in history, but that prose activates new lore
only on the next exchange.

Regenerating a group reply creates a same-speaker sibling under the original
parent. It does not replay downstream speakers. Fork-edit instead creates a new
user sibling and runs a fresh group exchange. Removed roster members are tombstoned
so old messages keep their names; re-adding the same card creates a new member
identity.

## Scene-local sheets

`group_members` carries two scene-local overrides, and they answer different
questions. `public_profile_override` is what the *rest of the cast* sees;
`card_sheet_override` is what the member reads about *itself*, standing in for
the card's `description` + `personality` join. Both resolve on `is not None`, so
a stored `""` blanks the field rather than falling back, and neither ever writes
the card — a card is a reusable asset that outlives the scene, and a scene that
edited it would change every other chat the card is in.

The sheet exists because a card asserts turn one forever and a scene does not:
hair is cut, a coat burns, a sword breaks. It can be typed by hand in Manage
cast, and — when the scene opts in — proposed from the played prose.

### The post-exchange pass

`conversations.group_sheet_updates` is the per-scene opt-in, **off by default**:
one billed call per member an exchange touched is not something a scene should start
paying for by existing, and staleness is a property of a *long* scene.

`pipeline/sheet_update.py` drives it in the `world_change` slot, gated on
`run_exchange_final` so it runs **once per exchange** on the members that actually spoke
— not cast-wide, which would bill a call per member per exchange to tell a silent
member nothing happened to them. `features/cards/sheet_update.py` owns the call:
one forced tool call per member, **never batched**, because a sheet is that
member's own material and B's sheet entering A's call would write B's secret
into a string A reads. The exchange's prose is shared evidence and goes into every
call; the sheets do not. Failures are swallowed at both levels — one member's
failed call never drops another's proposal, and the stage never costs the user
their reply.

Three rules shape what the pass is allowed to be:

- **It is offered under Private perspective only**, and the gate is the *turn
  driver's*, not the form's. Private is the one mode that reads a member's sheet
  from the trailing message, after history, where rewriting it every exchange costs
  no prefix rebuild. Under Shared and Swap the same text sits in the cached body
  ahead of the history, so an applied update rebuilds the whole scene prefix —
  the exact cost the opt-in is priced on avoiding. Leaving that invariant to the
  client alone meant a `PUT` changing only the mode left the pass running.
- **The evidence is the round, not the request.** An exchange is request-scoped, so
  under `Manual` — and for any cast-chip click on a resting scene — one round is
  several requests. The transcript is therefore the user's last message and every
  reply since (`entrypoints._round_prefix`), the same round
  `workflows/image_gen/subjects.py` reads. Only the *evidence* widens: the
  members proposed **about** stay this request's speakers, since an earlier
  request already billed a call for the ones it ran.
- **Nothing is applied.** The pass proposes; the user decides, in Manage cast.
  Same posture as Dynamic Worlds, for the same two failure modes: a bookkeeping
  model can disagree with a hand edit, and it can simply judge wrong.

### The review queue

`member_sheet_proposals` holds the staged rewrites — `pending` | `applied` |
`rejected` | `stale`. `base_sheet` is to a proposal what `worlds.content_revision`
is to a changeset: the apply takes `BEGIN IMMEDIATE`, re-resolves the member's
current effective sheet, and refuses (409) when it no longer matches. There is
no force-apply and no rebase, because a hand edit and a model's edit can
contradict each other in meaning even when both look reasonable.

**At most one pending proposal per member.** Every exchange stages against the sheet
as it stands, so two pending proposals for one member are necessarily derived
from the same base — applying either makes the other unapplyable, and regenerate
runs the exchange again and would stack a third. A new proposal therefore *replaces*
the member's pending one in place, keeping its row id, and the stage builds that
exchange's call on the replaced text so the drift accumulates into one reviewable
sheet instead of competing ones. A pending proposal whose `base_sheet` no longer
matches the stored sheet is not carried forward: the user hand-edited underneath
it, and resurrecting text they overwrote is the one thing staging must not do.

The listing route's default is the **review set** — `pending` plus `stale` — not
`pending` alone. A `stale` proposal is one the apply just refused, and it is
precisely the row that owes the user an explanation; fetching only `pending` made
it vanish at the moment of refusal. `?status=all` is the history view, and any
single status name is accepted.

Two lifecycle edges keep the queue honest. Removing a member from the scene
retires its undecided proposals in the same transaction as the tombstone —
Manage cast renders rows only for the active roster, so one left pending would
sit in the review count forever with no row to dismiss it from. And the client
fetches the queue whether or not the scene is currently opted in: turning the
pass off stops it *staging*, and must not also hide what it already staged.

### Where it surfaces

Manage cast is the review surface. Each member's row carries both overrides under
`Customize for this scene`, and its staged proposal below them — shown in full,
because the user is approving text rather than a label. A row with something
waiting opens by default and counts itself on its summary, and the cast rail's
`+ Manage cast` button carries the scene-wide count; a proposal nobody notices
never gets applied.

The override boxes are labelled by what they *reach*, which is mode-dependent for
the sheet: under Shared dossier a member's own sheet **is** its dossier, so the
words "what they read about themselves" would put the scene's only cross-member
disclosure behind the most private-sounding label on the screen. Under Swap it is
self-only for real — only the active speaker's card is sent.

## The rest of Orb in a group

A group is a conversation, so every feature that reads a conversation keeps
working; the ones that read *a character* have to be told which. Three answers
cover all of them, and which one applies is never the feature's own choice:

- **Scene-wide** — the feature belongs to the conversation, and the cast changes
  only what it reads. Lorebooks and Dynamic Worlds (every member's linked World
  is activated), the persona pin, macros, compression and checkpoints, the
  context-size counter, and card-embedded fragments, which merge across the whole
  cast (a global fragment still wins an id clash, and between two cards the first
  member in the roster keeps it).
- **Per exchange** — the feature belongs to the request, which is one Director
  decision and N replies. The Director, agentic lore selection, both
  direction-note placements and the Dynamic Worlds proposal run once per exchange
  rather than once per speaker (the two that read the finished prose — the
  end-of-turn note and the proposal — after the last one); a user's image upload
  is answered by every speaker in the exchange, not only the first.
- **Per speaker** — the feature belongs to a member, and the member is resolved
  from the reply it acts on. Editor passes (anti-slop, anti-repetition, length
  guard), feedback fragments, regenerate / super-regenerate / magic rewrite,
  image generation and TTS all run against the speaker of their own message.
  Their off-turn LLM calls still build the *scene's* prefix — the cast section
  and speaker-labelled history, resolved through the same `resolve_cast` the
  turn used — so the image prompter reads a transcript that says who did what,
  and rides the conversation's warm cache instead of evicting it.

Where a per-member setting has no message to resolve from — the image-generation
appearance profile, the TTS voice — the panel names the member instead: both open
on a **Cast member** selector, defaulting to the first member with a card, and
switching member reloads the form rather than writing to whoever spoke last.
`getGroupCast()` (workflow ABI v3) is how a workflow plugin reads the roster, and
`speaker_member_id` on the trigger route is how it addresses one member.

Image generation is per-speaker in *which member it is of* and per-round in *who
else it describes*. The speaker of the reply being visualized is the picture's
primary subject; the other members who spoke in the same **round** - the user's
last message and every reply since - **up to that reply** follow, in cast order. A
render never reads past its own anchor, so the first reply of a round has a shorter
cast than the last. It is the round, not the request: under **Manual** you give one
member the floor per click, and each click is its own request but the same round. Both halves of the render read
that one list. **One reference image per character** (see
[Reference images](../multimedia/image-generation.md#reference-images-in-a-group-chat)):
a cloud provider's reference field is one array, so it carries one likeness per
character in the shot, in cast order and never the same person twice; a ComfyUI
workflow's image inputs are structural and all get the speaker's. Whoever gets no
picture is described in full instead, and the prompter is told which image is whom,
so an unpictured character still comes out as themselves rather than as a generic
person. The camera does not change who is in the scene: first-person looks through
the *user's* eyes, and the user is a persona rather than a cast member, so every
character in the round is in front of the lens and none is dropped.

Character expressions follow the floor: the header's 👥 avatar opens the popup on
the member currently streaming, or the last one to have spoken, and switches face
mid-exchange as the floor moves.

The composer's local-model typeahead reads the scene the same way the turn does:
`{{char}}` is the title, its one-line summary is the roster, and each replayed
line is labelled with the member who said it rather than with the group.

## HTTP and SSE

Roster creation, conversion, and synchronization are transactional. The main
group routes are:

- `GET|PUT /api/conversations/{cid}/members`
- `POST /api/conversations/{cid}/convert-to-group`
- `POST /api/conversations/{cid}/group-conversation`
- `DELETE /api/conversations/{cid}/group`
- `POST /api/conversations/{cid}/speak`
- `POST /api/conversations/{cid}/activate`
- `POST /api/conversations/{cid}/members/scene-profile/generate`
- `PUT /api/characters/{card_id}/public-profile`
- `POST /api/characters/{card_id}/public-profile/generate`

Every group request emits one `speaking_plan`, then a
`speaker_start`/`speaker_done` pair per persisted reply, and exactly one
request-level `done`. The frontend creates and finalizes a bubble per speaker,
stops the previous audio channel at every `speaker_start`, and performs a full
message refetch/render after the exchange.

## Chat surface

The group screen shows four things: scene identity, conversation, cast and
composer. Everything else is contextual or lives in Group settings (`•••` in the
chat header), which owns the durable configuration — title, character context,
reply behavior, max replies per turn, scene premise, style instructions —
through `PUT /api/conversations/{cid}`. Cast membership, order, reply
eligibility and public-profile overrides are edited in Manage cast
(`PUT …/members`). The override box is one string with one meaning in every
mode; only its label changes, and under Shared dossier and Classic card swap it
is disabled with a one-line reason rather than accepting text that would never
ship on a turn.

Manage cast can also **generate** an override rather than have it hand-typed:
per row (`Draft` / `Redraft`) and cast-wide (`Draft scene profiles`, which fills
every empty one in sequence, opening each row as it lands and counting up so a
second click can stop it). Both go through
`POST /api/conversations/{cid}/members/scene-profile/generate`.

**One LLM call per member, never batched.** The context carries that member's
own card, its card-level `extensions.orb.public_profile` as the default to
adjust, the scene premise, and the other members' **names only** — bounded to
the first 16 in roster order, with `Other cast members omitted from this draft:
N` appended when there are more (a prompt-size guard, not a roster limit).
Putting member B's card into member A's draft would write B's secret into a
string every member reads under Private perspective, which is the one thing that
mode promises cannot happen;
`test_scene_profile_draft_sends_only_the_target_card_and_other_names` is the
executable form of the rule. Names arrive from the client because the modal is
client-side until Save — a member added seconds ago exists only in the DOM — and
are treated as untrusted display text: stripped and capped, never sorted or
deduplicated.

The output is the same `Appearance: …` / `Role: …` two-liner
`database.queries.character_cards.render_public_profile` renders from a card, so
an overridden member and a non-overridden one read identically in the assembled
prompt. The endpoint is deliberately **mode-blind** — `PUT …/members` accepts an
override under every mode, so a generate route that refused would leave the two
halves of one field disagreeing about whether the mode is a server rule; the
gating is the UI's. Nothing is persisted: Save cast is still what writes it.
Failures raise (502/500) rather than degrading to a plausible-looking draft,
because a loop that writes N overrides the user saves in one click cannot afford
a fabricated one.

The cast rail sits on top of the composer and is the only reply control there is
— there is no separate strategy line. One click on a chip does one of two
things, and the scene decides which (`group_cast.js:castClickSpeaksNow` is the
only definition):

- **Resting scene** — nothing streaming, nothing drafted or attached: the member
  takes the floor immediately via `POST …/speak`. There is no toggle here; the
  click resolves the pick by using it, and the one-shot cleanup drops the pin
  afterwards outside `Manual`.
- **Busy or drafted**: the click only queues that member as the next speaker.
  Clicking whoever is already queued takes the pick back.

Muted members render disabled. In `Manual` mode the send button stays disabled
until a member is picked and says so in its tooltip, since nothing else on
screen explains the block.

Convert-to-group does not offer the context control: it starts on Private
perspective and the setting is one click away afterwards. New group chat keeps
the control under **Advanced**, still defaulted to Private, and adds a
**recommendation** under the cast picker.

`group_cast.js:recommendContextMode` is the only place that rule lives. It is a
choice between two modes — Shared dossier is never recommended, being a
deliberate privacy decision rather than a cost one — and it turns on the two
things creation already knows: how many characters are picked, and how heavy
their cards are. The two modes fail in opposite directions:

- **Private perspective** keeps the shared body tiny (one public profile per
  member) but puts the speaking card in the trailing message, *after* history —
  the one place a prefix cache can never reach, so that card is re-read on every
  writer and editor call. Its cost tracks **card size** and is flat in cast size.
- **Classic card swap** parks the speaking card *before* history where it caches,
  but makes each character its own cache lineage. Its cost tracks **cast size**
  and is nearly flat in card size.

So swap wins only when the cast is narrow enough to keep a branch per character
warm *and* the cards are heavy enough to be worth caching: `mean card tokens >=
500 x (cast - 1)`, capped at three members, where a server holding several
prefix lanes starts thrashing (swap needs roughly 2.5 lanes per member — 5 at
two, 8 at three, 10 at four — so a fourth member is a 4-6x jump, not a drift).
The mean is the right statistic because both modes bill per speaking turn: one
2000-token card beside two 500s costs what three 1000s cost, to within a token.

Nothing is recommended below **two** members. The threshold is zero at one
member, so every card cleared it and an eight-token stub was told it was heavy
enough to cache; there is also no second card to weigh it against. And the panel
recomputes on every pick, so advising at one member means answering for a cast
the user is still assembling. A genuine one-member group does leave measured
savings unclaimed — it is also a solo chat with extra steps. From two members up
the threshold is never below 500, so a cast of empty or narrator-shaped cards
falls to Private on the comparison itself and needs no separate floor.

The rule is deliberately asymmetric, and every case it gets wrong it gets wrong
toward Private — recommending swap on a cast too wide for the cache costs
multiples, while recommending private where swap was marginally better costs at
most ~1.3x. Private is also the safer default on meaning: the only mode with a
privacy boundary, and the only one where characters know anything about each
other beyond names. The panel therefore states the trade as well as the
recommendation, and only ever offers — the user applies it, and applying it
opens Advanced so the control is never seen to disagree with itself.

The weight comes from `def_chars` on the library list: `description +
personality + mes_example` summed server-side, one integer per card, so the list
path can answer "how heavy is this card" without shipping the bodies it
deliberately omits. A card's `post_history_instructions` is excluded because
every mode keeps it in the speaker's trailing message and it therefore cannot
discriminate.

The speaking-plan rail is painted only while an exchange with two or more speakers is
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
