# Scene Direction

The **Director** is an optional model pass that runs before the Writer. It reads
the conversation and prepares a **Scene Guidance** block with guidance about
mood, pacing, focus, or other details. The Writer sees the block; it is hidden
from the conversation view and available in the Inspector.

You control the Director with **fragments**. Orb includes default fragments, and
you can add, edit, reorder, or disable them.

## Mood fragments

A mood fragment is an instruction that the Director turns on or off for a turn.
Think dynamic prompt management. For example, a `Terse` mood can tell the Writer
to use short sentences during a tense scene.

Each mood has:

- **ID**: an internal name using letters, numbers, underscores, or hyphens
- **Label**: the display name shown in the sidepanel
- **Description**: when the Director should use the mood
- **Prompt text**: instructions sent to the Writer while it is active
- **Negative prompt**: optional instructions sent to the Writer when the mood is turned off

When a cooldown makes a mood unavailable, Orb pauses it without sending its
negative prompt. Negative prompts are reserved for ordinary Director-driven
deactivation.

## Interactive fragments

An interactive fragment is a value the Director fills in for the current turn.
For example, a `pacing` fragment might produce `slow burn` or `time skip`.

Each interactive fragment has:

- **ID**, **Label**, and **Description**, with the same purpose as a mood fragment.
  `moods` and `retire` are reserved, because the Agent's tools already use them.
- **Injection label**: the heading shown to the Writer, such as `Pacing:`
- **Field type**: the shape of the value
- **Required**: whether the Director must provide a value every turn

The Description is read by the Director. The Injection label is read by the
Writer. Describe the value you want and include examples when useful.

### Field types

| Type | Use |
|---|---|
| **Single** | One text value, such as `Pacing: slow burn`. |
| **List** | Several values shown as a list, such as active plot threads. |
| **Feedback** | A note shown to you after the reply. See [Feedback Fragments](feedback-fragments.md). |
| **State** | A value or a list kept across turns on the conversation branch, such as trust, an inventory, or open threads. See [State Fragments](state-fragments.md). |
| **Post-processing** | An editing instruction applied to the completed reply. See [Post-processing Fragments](post-processing-fragments.md). |
| **Decision** | A question the Judge answers before the Director runs, contributing guidance the Director plans around. See [Decision Fragments](decision-fragments.md). |

## Macros and order

Fragment text supports [macros](macros.md). A random macro in mood prompt text
rolls once per conversation. A random macro emitted in a Director value rolls on
each turn. Put a macro in single backticks when you want the Director to see it as
literal text.

Fragments run from top to bottom within their stage. Earlier Director values can
provide context for later values, and the Writer sees them in the same order.
Post-processing fragments run afterward as Editor tasks and do not enter the
Director schema or Scene Guidance.

## Inspector in the chat

The Inspector shows one reply's turn details at a time. Turn on **Settings →
Show Inspector in chat** to show each reply's details in the chat instead. The
reply's reasoning gets its own **Reasoning** block above its text, with a tab for
each pass that wrote some. The **Inspector** block between the Reasoning block
and the text holds the rest: moods, agent latency, decisions, feedback, state changes, tool calls and
the injection block. A section appears only when the reply has something for it,
and a reply with nothing to show gets no block.

A collapsed Inspector block keeps one summary line with the active moods and
decision outcomes. Opening or collapsing either block, or any section in one,
applies to every reply and the choice is saved. Sections the Inspector panel
shows too follow along there; the Reasoning block opens and closes apart from
the panel's Reasoning controls. The
panel keeps the context size and the reasoning controls for the next turn.
