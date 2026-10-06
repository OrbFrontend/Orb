# Agentic Lorebook

An Agentic Lorebook lets the **Director** choose lorebook entries by reading the
scene. It can find relevant lore even when none of the entry's keywords appear.

## How it works

A [World](lorebooks.md) contains lorebook entries. An entry can be active because
it is:

- **Constant**: always included in the character context.
- **Keyword-activated**: included when its keywords match recent messages, while Agentic Lorebook is off.
- **Selected by the Agent**: chosen by the Director for the current scene.

When Agentic Lorebook is on, the Director has full control over non-constant
entries. Keyword activation is disabled, so only the Director's picks are
included. An empty selection or a failed selection call includes no non-constant
entries. Constant entries stay active.

## What the agent sees

How does the agent decide which entries are relevant? These info will be sent to it:

- The lorebook's name
- The entries' names
- Each entry's keywords as relevance hints (up to three, excluding the entry name)

Constant entries are excluded from both the catalog and the selection call's
system prompt. They remain in the ordinary Director, Writer, and Editor context.

## Enable it

Open **Settings → Agents** and turn on **Agentic Lorebook**. The global **Agent**
toggle must also be on.

The Director receives a short catalog of non-constant entries and selects the
ones that fit the current scene. This uses one additional lightweight model call
per turn. If there are no selectable entries, no selection call is needed.
Turning off Agentic Lorebook or the global Agent restores normal keyword activation.

See [Lorebooks](lorebooks.md) for entry types, triggers, macros, and import rules.
