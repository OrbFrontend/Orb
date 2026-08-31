# Scene Direction

The **Director** is an optional model pass that runs before the Writer. It reads
the conversation and prepares a **Scene Direction** block with guidance about
mood, pacing, focus, or other details. The Writer sees the block; it is hidden
from the conversation view and available in the Inspector.

You control the Director with **fragments**. Orb includes default fragments, and
you can add, edit, reorder, or disable them.

## Mood fragments

A mood fragment is an instruction that the Director turns on or off for a turn.
For example, a `Terse` mood can tell the Writer to use short sentences during a
tense scene.

Each mood has:

- **ID**: an internal name using letters, numbers, underscores, or hyphens
- **Label**: the name shown in Orb
- **Description**: when the Director should use the mood
- **Prompt text**: instructions sent to the Writer while it is active
- **Negative prompt**: optional instructions sent when the mood is turned off

## Interactive fragments

An interactive fragment is a value the Director fills in for the current turn.
For example, a `pacing` fragment might produce `slow burn` or `time skip`.

Each interactive fragment has:

- **ID**, **Label**, and **Description**, with the same purpose as a mood fragment
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
| **Progressive** | A value that changes gradually across turns, such as trust or tension. |
| **Feedback** | A note shown to you after the reply. See [Feedback Fragments](feedback-fragments.md). |
| **Direction note** | A note saved on the conversation branch. See [Direction Notes](direction-notes.md). |

## Macros and order

Fragment text supports [macros](macros.md). A random macro in mood prompt text
rolls once per conversation. A random macro emitted in a Director value rolls on
each turn. Put a macro in single backticks when you want the Director to see it as
literal text.

Fragments run from top to bottom. Earlier values can provide context for later
values, and the Writer sees them in the same order.
