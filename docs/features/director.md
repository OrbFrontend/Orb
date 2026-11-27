# Scene Direction

The **Director** is an optional model pass that runs before the Writer. It reads
the conversation and prepares a **Scene Guidance** block with guidance about
mood, pacing, focus, or other details. The Writer sees the block; it is hidden
from the conversation view and available in the Inspector.

You control the Director with **fragments**. Orb includes default fragments, and
you can add, edit, reorder, or disable them. Fragments belong to the active
[profile](profiles.md): deleting one removes it from that profile only.

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

When Direction or the Agent is off, new replies have no active moods. Existing
cooldowns still age, and saved state fragments still inject as configured.

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
| **Recast** | An editing instruction applied to the completed reply. See [Recast Fragments](recast-fragments.md). |
| **Rewrite** | An instruction the Editor follows to rewrite the whole completed reply. See [Rewrite Fragments](rewrite-fragments.md). |
| **Decision** | A question the Judge answers at its place in the list: above the Director fragments it settles something the Director plans around, between them it rules on the plan so far, below them it judges the finished plan for the Writer. See [Decision Fragments](decision-fragments.md). |

## Macros and order

Fragment text supports [macros](macros.md). A random macro in mood prompt text
rolls once per conversation. A random macro emitted in a Director value rolls on
emission, and the Judge, later Director steps and the Writer all read that roll.
A `pick` there stays fixed for that field. Put a macro in single backticks
when you want the Director to see it as literal text.

The **Judge & Director** list runs top to bottom. Each Director request sees
the Questions of eligible decisions below the fields it fills, and the guidance
from **Director** or **Both** decisions above them, in order. The Director also
sees every **Writer** Question, wherever it sits. Only the Writer receives
Writer-only answers.
The final moods and one-value state request and the before-Writer state update
both see closing Questions.

With **Individual fragment processing** off, the Director can fill several
fields in one call. Each middle place where a Director ruling can affect later
fields costs one extra Director call. An answer settled at the start only
splits the call if it has guidance. A decision waiting for the plan adds a
near-instant Judge request when asked. Writer-only waits read the fields
through their place from the same combined call. Known skips add no call.
Individual processing already gives each field its own call.

Recast and rewrite fragments run afterward as Editor tasks and do not enter the
Director schema or Scene Guidance.

## Inspector in the chat

**Settings → Show Inspector in chat** shows each reply's turn details above its
text instead of one reply at a time in the Inspector panel. A **Reasoning** block
has a tab for each pass that wrote some; the **Inspector** block under it holds
moods, agent latency, decisions, feedback, state changes, tool calls and the
injection block. Empty sections and empty blocks are left out.

**Agent Latency** expands into grouped timings for Director fragments, lorebook
selection and update proposals, Writer, and Editor steps and fragments. Individual
fragment mode shows each Director fragment; a combined call shares one timing.
Editor fragment timings include their gate checks, with skipped, stopped, or
failed steps labeled. The collapsed section keeps the total visible. The
breakdown uses the full in-chat panel width, wraps long fragment names, and
aligns durations on the right. Older replies keep their saved total and explain
when no breakdown is available.

A collapsed Inspector block keeps a summary line of active moods and decision
outcomes. Opening or collapsing a block or section applies to every reply and to
the panel, and is saved; the Reasoning block opens apart from the panel's
Reasoning controls. The panel keeps the next-turn reasoning controls; the context size sits
in the character portrait under This chat.
