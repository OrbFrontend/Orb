# Lorebooks

A **lorebook** is a pile of notes about your setting. Each note is an **entry**: a name, some trigger keywords, and the text to inject. Entries live in a **World**, which is just a folder you can switch on and off.

Orb reads SillyTavern lorebooks, but it does not behave exactly like SillyTavern. This page is the list of differences. If you have never used SillyTavern, read it as "how lorebooks work here".

## Worlds are on or off, globally

A World is either enabled or disabled. Every enabled entry in every enabled World is live, for every chat.

You can link a World to a character card. When you open that character's chat, Orb turns its World on and turns the previous character's World off. That is the whole "per-character lorebook" story — there is no filtering deeper in the pipeline.

Entries and Worlds each have their own enable toggle. Both must be on.

## Three places an entry can be inserted

SillyTavern has many insertion positions. Orb has three, and you pick between them with two checkboxes.

| Entry | Where the text goes |
|---|---|
| Normal (keywords) | In the tail, just before your latest message |
| **Constant** | In the system prompt, under the character description, as a `## Lorebook` section |
| **Constant + @ Depth** | In the tail, *after* your latest message — closest to what the model writes next |

@ Depth only appears once **Constant** is checked.

Rules of thumb:

- Background facts the model should always know → **Constant**.
- Stuff that only matters when a topic comes up → keywords.
- Rules you want obeyed *right now* ("keep replies under 200 words") → **Constant + @ Depth**.

## How keywords trigger

Orb glues together the last **6 messages** and looks for your keywords in that text.

- Plain substring match. `cat` matches `catapult`. Keep keywords specific.
- Case-insensitive by default. Uncheck **Case sensitive** on the entry to change that.
- **Regex** — check it and each keyword is treated as a regular expression. A broken pattern quietly falls back to substring.
- **Selective** — the entry also needs one of its *secondary* keywords to appear. Both lists must hit.

That is the entire trigger system. Things SillyTavern has and Orb deliberately does not:

| Not in Orb | What happens instead |
|---|---|
| Recursion (entries triggering entries) | Only real messages are scanned, one pass |
| Token budget | Every active entry is injected; you control the size |
| Probability / sticky / cooldown / delay | An entry is active or it is not |
| Per-entry scan depth | Always 6 messages (2 in agentic mode — the Director already read the rest) |
| Inclusion groups, min activations, character filters | Nothing equivalent |

Fewer knobs, and what you see in the editor is what the model gets.

## Ordering

Active entries are always rendered in the same order: **priority** high → low, then **insertion order** low → high, then by age.

The order never depends on which entry triggered first. That is on purpose — a stable block means the prompt prefix stays byte-identical between turns, which is what keeps [KV cache reuse](../architecture/kv-cache.md) working.

## Macros inside entries

All the usual [macros](macros.md) work in entry names and text. *When* they roll depends on where the entry lands:

| Entry | `{{roll}}`, `{{random}}`, `{{pick}}` |
|---|---|
| Constant (system prompt) | Rolled once per conversation, then frozen |
| Keyword-triggered | Rolled once per conversation, then frozen |
| Constant + **@ Depth** | **Fresh every turn** |

The freezing is not a limitation, it is the point: a re-rolling system prompt would change the cached prefix every turn and throw away the KV cache. @ Depth sits after the cache boundary, so it can roll freely.

!!! tip "Rolling dice every turn"
    Want `Today's weather: {{pick::rain::fog::clear}}` to change each turn? Make the entry **Constant + @ Depth**. Anywhere else it picks once and stays.

## Letting the Director choose

Keywords are blunt — a scene can circle a topic without ever naming it. Turn on [Agentic Lorebook](agentic-lorebook.md) and the Director picks entries by reading the scene. The keyword scan keeps running alongside it, so the Director can only *add* entries, never suppress a keyword hit.

## Letting the Agent write entries

Flip **Dynamic World** on a World and the Agent proposes new and revised entries from what happens in play — you review each one before it lands, and it can never touch an entry you wrote. See [Dynamic Worlds](dynamic-worlds.md).

## Import and export

Orb reads and writes the standard V2 `character_book` JSON: **Worlds → Import / Export**, and books embedded in a character PNG are imported into a new World on card import.

Field mapping, in case something looks off after an import:

| In the file | In Orb |
|---|---|
| `keys` / `key` | Trigger keywords |
| `secondary_keys` / `keysecondary` | Secondary keywords |
| `name` or `comment` | Entry name |
| `disable` / `enabled` | Enabled toggle |
| `insertion_order` | Insertion order (and priority, if no `priority`) |
| `case_sensitive` / `extensions.case_sensitive` | Case sensitive |
| `position: 4` / `extensions.position: 4` | @ Depth |

SillyTavern writes the same fields in two different places depending on which button you pressed: a **World Info** export keeps them at the top level, while a **character card's** embedded book moves them into each entry's `extensions`. Orb reads both, so either file imports the same way.

Two quirks worth knowing:

- Entries that set **selective** but list no secondary keywords are common in the wild, and taken literally they would match nothing. Orb drops the flag instead.
- V3 decorators (the `@@` lines at the top of an entry's content) are stripped on import. Orb does not implement them.

### What survives a round-trip

Export writes a valid V2 book, with the SillyTavern-specific fields filled in where SillyTavern looks for them. Everything both apps support survives a trip in either direction: keywords, secondary keywords, content, entry name, enabled, constant, insertion order, case sensitivity, and **@ Depth**.

What is lost is only what Orb has no equivalent for. Import a book that uses recursion, probability, sticky/cooldown/delay, groups, roles, per-entry scan depth, or the inverted `NOT_ANY` selective logic, and those settings do not come back out — Orb never stored them.

!!! note "@ Depth details"
    Orb exports @ Depth entries as SillyTavern's `position: 4` with `depth: 0`, because Orb always injects them immediately after the latest message. SillyTavern lets you choose the depth; if you set it to something else and bring the book back, Orb reads it as plain @ Depth again.
