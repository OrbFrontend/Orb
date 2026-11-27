# Profiles

A **profile** is a named setup you switch between from the picker under the Orb
title. Each profile owns:

- the pipeline switches: Agent, tools, Length Guard, agentic lorebook, individual
  Director fragments, Prevent prompt overrides, reasoning, and the Editor's audit
  checks
- the model each lane uses: the Writer's and the Agent's endpoint and model, and
  the Judge's endpoint and model
- its own [Prompt Layout](prompt-layout.md)
- its own fragments: mood, state, decision, recast, and rewrite
- its own [phrase bank](anti-slop.md), with its suggestions and dismissals
- which [Worlds](lorebooks.md) are global

Each profile has its own copy of these. Editing, switching off, or deleting a
fragment, a phrase, or a global mark changes only the active profile. A fragment
keeps its ID in every profile, so a chat's saved state for it carries across a
switch.

Shared by every profile:

- Worlds and their entries
- sampler values (temperature and the rest), which belong to the model
  configuration
- characters, the fragments inside character cards, chats, and documents
- personas, endpoints, and the other settings: your name, display options,
  Document mode, the assistant, workflows, and Local ML

Changes save to the active profile as you make them; there is no Save button.
Switching profiles loads the other profile's setup in place. The picker is off
while a reply is being written, and switching, creating, or deleting a profile
waits while a chat, a document, or the assistant is generating.

## Manage profiles

The picker's list ends with:

| Entry | Action |
|---|---|
| **New…** | A new profile with the built-in layout, fragments, and phrase bank, and no global Worlds |
| **Copy…** | A copy of the active profile and everything it owns |
| **Rename…** | Rename the active profile |
| **Delete…** | Delete the active profile and everything it owns; Orb switches to the first one left |

The last profile cannot be deleted. Before deleting a profile, Orb saves an
`auto` [backup](backups.md) of its setup, fragments, and phrase bank.

If the Writer's or the Agent's endpoint or model a profile uses is deleted,
switching to that profile uses another endpoint with the same URL and a model
configuration with the same model name, and otherwise leaves that lane as it is.
A profile without a Judge, or whose Judge endpoint was deleted and has no other
with its URL, turns the Judge off.

**Reset to defaults** resets the active profile and the shared settings and
endpoints. Other profiles keep theirs.
