# Group Chats

A group chat puts several characters into one scene. They share the conversation, the lore, and a single premise — and each of them stays itself. Two decisions shape every exchange: **who replies**, and **what each character is told** about the others.

## Creating a group

Pick **New group chat** in the sidebar and choose your cast. The order you pick them in becomes the default speaking order.

Under the cast picker sits a **recommendation**: Orb weighs how many characters you picked against how heavy their cards are, and suggests **Private perspective** or **Classic card swap** for the Character context setting (see below). It only ever offers — nothing changes until you apply it, and applying opens Advanced so you can see exactly what changed. With fewer than two characters picked there is no recommendation; there is also no second card to weigh anything against.

!!! tip
    Already mid-story in a solo chat? Look for **Convert to group** among the conversation's actions. It starts on Private perspective; every other setting is one click away afterwards.

## Scene setup

Everything about a scene lives behind the cast rail's **+ Manage cast** button, which opens **Scene setup** with two tabs:

- **Group settings** — title, character context, reply behavior, max replies per turn, the scene's premise, style instructions, and delete.
- **Cast** — who's in, their order, who can speak, each member's per-scene customization, and any staged sheet updates waiting for review.

One Save writes both tabs, and only the fields you actually changed — so a title typed on one tab can't be lost by saving from the other.

## Who replies next

Reply behavior decides who answers your message:

| Behavior | What happens |
|---|---|
| **Auto — Director chooses** | A Director pass reads the scene and plans the replies — who speaks, and in what order. It may also decide nobody speaks at all. |
| **Rotate — Cast replies in order** | Each turn goes to the next eligible member down the roster. |
| **Manual — Select every reply** | You click who answers. The send button stays disabled until you do, and says so. |

**Max replies per turn** caps how many characters can answer within one exchange under Auto.

### Cast chips

The chips above the composer are the only reply control on the chat screen:

- **Nothing streaming and nothing queued?** Clicking a chip hands that member the floor immediately.
- **While someone streams or is queued?** Clicking queues that member as the next speaker. Click the queued chip again to take the pick back.
- Muted members sit greyed out.

A pick made outside Manual clears itself once it's been used, and the configured strategy resumes on its own. If an exchange fails or is stopped, the pick survives so you can retry it.

When several replies are planned you'll see them announced as each one starts. When nobody is planned — no pick made in Manual, or the Director rests the scene — a toast tells you: your message stands, and no one answers it yet.

## What each character knows

**Character context** (in Group settings) decides how much of each card the rest of the cast gets to see:

| Mode | What it means |
|---|---|
| **Private perspective** (default) | A character's own card text is private to it. Everyone else knows it only through its public profile. |
| **Shared dossier** | Every character reads every other's actual card text. Nothing held back — the most capable mode, and the most revealing. |
| **Classic card swap** | Like classic frontends: whoever is speaking gets their full card swapped in; the rest of the cast knows each other through public profiles. |

Whichever mode you pick, three things always hold:

- **One premise.** Cards' own scenarios and system-prompt overrides are ignored — the scene has exactly one premise, set in Group settings. Swap changes who is speaking, not whose instructions rule the room.
- **Names travel.** Roster names show up in the cast list and on speaker labels whatever else is hidden. Card-linked Worlds and [card-embedded fragments](card-fragments.md) belong to the whole scene too — there is no per-character private lore.
- **Each voice keeps its own instructions.** A card's post-history instructions still steer its own speaker and nobody else. The scene-wide style-instructions box covers everyone equally.

### Macros and personas

[Macros](macros.md) keep their meaning in groups: `{{cast}}` always expands to the roster names, and `{{char}}` means the group's title everywhere except inside a member's own card-like text — where it means *that member*. A card written "{{char}} never lies" still describes itself, not your scene title.

Personas follow the chat, not the cards: the [pinned persona](persona-pinning.md) applies to the whole scene, and card-level persona locks don't compete.

## Public profiles

Under Private perspective and Classic card swap, what the cast knows about a member comes from its public profile — an **Appearance / Role** two-liner Orb renders from the card by default.

Override it per member under **Customize for this scene** in Manage cast. Type it yourself, press **Draft** / **Redraft** to generate one from that member's own card, or **Draft scene profiles** to fill every empty profile in sequence — a counter runs while it works, and a second click stops it. A draft sees only that member's card plus the other members' names, never anyone else's secrets, and if generation fails you'll see an error rather than a plausible-looking guess. Nothing saves until you press Save.

Under **Shared dossier** these boxes are disabled: everyone already reads the real thing, so a curated version would change nothing.

## Scene sheets

A card asserts itself forever; a scene doesn't. Hair gets cut, coats burn, swords break. Each member therefore has a second override — what *it* reads about **itself** — which stands in for its card's description and personality inside this scene only. The card itself is never touched; every other chat using it stays exactly as it was.

Write it by hand, or let Orb propose updates from play:

1. Turn on the sheet-update toggle in Group settings. It's **off by default** (each exchange bills one extra call per member who spoke) and offered under Private perspective.
2. After an exchange, Orb drafts updated sheets from what happened since your last message. At most one proposal waits per member — a later exchange refreshes it in place rather than stacking up a queue.
3. Review in Manage cast. A member with something waiting opens by default, shows the proposed sheet in full, and offers **Apply** / **Reject**. The **+ Manage cast** button counts what's waiting scene-wide, so a proposal can't hide.

If you hand-edit a sheet while a proposal waits, that proposal is marked **stale** rather than silently fighting your edit — it stays listed, with the reason it can no longer be applied. Removing a member retires its undecided proposals. Turning the toggle off stops new proposals; ones already staged stay visible until you decide them.

## Regenerating, branching, checkpoints

- **Regenerate** writes a fresh alternative by the same character at the same point. Characters who spoke after it aren't replayed.
- **Fork-edit** starts a fresh exchange from an edited message of yours.
- **Checkpoint** and **Compress History** carry the whole cast along with the history — a checkpoint snapshots the scene, not just the words — and stay under the same sidebar entry. Compression summaries render as plain **Summary** bubbles.
- Removing a member keeps its old messages' names and avatars. Adding the same card again creates a fresh identity, not a reunion.

The sidebar paints **one row per group**, however many branches exist — the group's name, its cast, and a count once it branches. That row's × deletes the whole family at once: a group has no existence apart from its conversations. In the composer's ☰ menu, **New conversation** starts an empty scene with the same cast, and **Conversations** lists the family's branches.

## Everything else in a group

| Feature | In a group |
|---|---|
| [Director](director.md), [direction notes](direction-notes.md) | Run once per exchange, steering all replies together — not once per speaker. |
| [Lorebooks](lorebooks.md) & [Dynamic Worlds](dynamic-worlds.md) | Scene-wide: every member's linked World participates. |
| [Card-embedded fragments](card-fragments.md) | Merge across the whole cast. |
| Editor passes — [anti-slop](anti-slop.md), [anti-repetition](anti-repetition.md), [length guard](length-guard.md) | Each reply uses its own speaker's settings. |
| Images you upload | Answered by every speaker in the exchange. |
| [Image generation](../multimedia/image-generation.md) | The reply's speaker is the subject, followed by whoever else spoke in the round up to that point. Panels opened outside a reply ask which **Cast member** they're for — see [reference images in groups](../multimedia/image-generation.md#reference-images-in-a-group-chat). |
| [TTS](../multimedia/tts.md) | Voice panels open on a **Cast member** selector too. |
| [Character expressions](character-expressions.md) | The avatar popup follows whoever holds the floor. |
| Context-size counter | Measures the largest single call a group exchange makes — never the sum across speakers. |

!!! tip "Cost"
    The context modes bill differently: Classic card swap caches well but wants one cache lane per character, while Private perspective shares one trunk and re-reads only the speaking card each turn. The creation recommendation does this math for you; the details live in [KV Cache Reuse](../architecture/kv-cache.md) and the [Group Chats architecture notes](../architecture/group-chats.md).
