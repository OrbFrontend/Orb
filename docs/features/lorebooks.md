# Lorebooks

A lorebook is a collection of setting notes. In Orb, entries belong to a **World**.
Each entry has text, an enabled state, and optional trigger keywords.

## Worlds and entries

Worlds are enabled globally. Every enabled entry in an enabled World is available
to chats. You can link a World to a character; opening that character's chat
enables the linked World and disables the previous character's linked World.
Entries and Worlds must both be enabled.

## Where Orb inserts an entry

Choose the placement with **Constant** and **@ Depth**:

| Entry type | Prompt location | Good for |
|---|---|---|
| Keyword-activated | Near the end of the prompt, before your latest message | Facts that matter when a topic appears |
| **Constant** | In the system prompt under the character description | Facts that should always be available |
| **Constant + @ Depth** | After your latest message | Instructions that should be close to the next reply |

`@ Depth` is available after **Constant** is enabled.

## Keyword triggers

Orb scans the latest six messages for keywords.

- Matching is case-insensitive by default and uses substring matching.
- **Case sensitive** changes the case behavior.
- **Regex** treats each keyword as a regular expression. An invalid expression
  falls back to substring matching.
- **Selective** also requires a match from the secondary-keyword list.

Orb does not implement recursion, token budgets, probability, sticky or cooldown
rules, inclusion groups, roles, per-entry scan depth, or character filters. Each
enabled entry either matches and is inserted or does not.

Active entries use a stable order: priority, insertion order, then age. This keeps
prompt content predictable across turns.

## Macros

[Macros](macros.md) work in entry names and text. Random macros in constant and
keyword-activated entries roll once per conversation. A constant entry with
`@ Depth` rolls on every turn.

## Agentic and dynamic Worlds

[Agentic Lorebook](agentic-lorebook.md) lets the Director add entries by reading
the scene. Keyword matches still apply. [Dynamic Worlds](dynamic-worlds.md) lets
the Agent propose new, revised, or retracted entries for your review.

## Import and export

Use **Worlds → Import / Export** for V2 `character_book` JSON. An embedded
character-card lorebook imports into a new World.

| File field | Orb field |
|---|---|
| `keys` or `key` | Trigger keywords |
| `secondary_keys` or `keysecondary` | Secondary keywords |
| `name` or `comment` | Entry name |
| `disable` or `enabled` | Enabled state |
| `insertion_order` | Insertion order and, when needed, priority |
| `case_sensitive` or `extensions.case_sensitive` | Case sensitivity |
| `position: 4` or `extensions.position: 4` | `@ Depth` |

Orb reads these fields from both top-level and embedded SillyTavern formats. It
removes V3 decorators and does not preserve SillyTavern options that Orb cannot
use. Export preserves the common fields: keywords, secondary keywords, content,
name, enabled state, constant placement, insertion order, case sensitivity, and
`@ Depth`.
