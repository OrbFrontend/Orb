# Agentic Lorebook

An Agentic Lorebook lets the **Director** choose lorebook entries by reading the
scene. It can find relevant lore even when none of the entry's keywords appear.

## How it works

A [World](lorebooks.md) contains lorebook entries. An entry can be active because
it is:

- **Constant**: always included in the character context.
- **Keyword-activated**: included when its keywords match recent messages.
- **Selected by the Agent**: chosen by the Director for the current scene.

Agentic selection adds to the normal rules. Constant entries stay active, and the
keyword scan still runs. The Director cannot remove a constant entry or cancel a
keyword match.

## Enable it

Open **Settings → Agents** and turn on **Agentic Lorebook**. The global **Agent**
toggle must also be on.

The Director receives a short catalog of non-constant entries and selects the
ones that fit the current scene. This uses one additional lightweight model call
per turn. If there are no selectable entries, Orb uses the normal keyword scan.

See [Lorebooks](lorebooks.md) for entry types, triggers, macros, and import rules.
