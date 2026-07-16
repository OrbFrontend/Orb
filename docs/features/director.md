# Scene Direction

Every turn, before the Writer starts, the **Director** model reads the conversation and decides how the next reply should go — the mood, the pacing, what to focus on. It writes its decisions into a **Scene Direction** block that is quietly attached to the Writer's prompt. The Writer follows it; you never see it in the chat (the Inspector shows it if you're curious).

**Fragments** are the questions the Director answers each turn. Orb ships a default set, and you can add, edit, reorder, or disable them freely. There are two kinds: **mood fragments** (on/off switches) and **interactive fragments** (fill-in-the-blank values).

## Mood Fragments

A mood fragment is a block of instructions the Director can switch **on or off** each turn. Think of SillyTavern's user-defined macros, except an agent decides when to apply them.

Example: a mood called *Terse*. When the Director senses the scene calls for it, it activates the mood, and its prompt text ("Write tersely. Short sentences. No flowery language.") lands in the Scene Direction block. When the scene moves on, the Director switches it off again.

Each mood fragment has:

- **ID** — the name the Director uses internally in its tool call. Keep it short: letters, numbers, underscores.
- **Label** — the display name you see in the sidebar list.
- **Description** — tells the Director *when* this mood is appropriate. Write it like advice to a co-author: "Use when the scene turns tense or violent."
- **Prompt text** — the actual instructions sent to the Writer while the mood is **active**.
- **Negative prompt** — sent to the Writer once, when the mood has just been switched **off**, so it knows to dial the style back ("Stop using short, clipped sentences."). Optional.

The defaults cover writing style, but a mood can carry any instruction you want toggled dynamically — content rules, camera perspective, anything.

## Interactive Fragments

Where moods are on/off switches, an interactive fragment is a **blank the Director fills in with a fresh value every turn**. These can be compared to the status tracking blocks you'd see in some sophisticated character cards. But rather than reflecting what already happened, they act as a forward-looking game plan that shapes what the Writer produces.

Say you create a fragment with ID `pacing`. Each turn the Director picks a value ("slow burn", "time-skip", …), and the Writer receives it in the Scene Direction block as:

```
Pacing: slow burn
```

Each interactive fragment has:

- **ID** — the name the Director uses internally in its tool call. Keep it short: letters, numbers, underscores.
- **Label** — the display name you see in the sidebar list and the Inspector.
- **Injection Label** — the heading the *Writer* sees in front of the value (the `Pacing:` in the example above). Usually the same as the Label; leave it blank to reuse the Label. For feedback fragments it heads the note shown to *you* instead.
- **Description** — instructions to the *Director* on how to fill the blank. This is the most important field: the Director sees only the ID and this text, so say what kind of value you want and give an example or two.
- **Field Type** — what shape the value takes; see below.
- **Required** — if checked, the Director must always supply a value. Unchecked fragments may be left blank on turns where they don't apply.

!!! tip "Who reads what"
    A common point of confusion: the **Description** talks to the Director (what to decide), the **Injection Label** talks to the Writer (how the decision is presented). The fragment editor shows an example placeholder for every field, and they change when you switch the field type.

### Field types

- **Single** — one plain text value. Rendered as `Injection Label: value`.
- **List** — several values, rendered as a bullet list under the label. Good for things that are naturally plural: active plot threads, characters in the scene.
- **Progressive** — a value that **persists and evolves across turns**. The Director sees last turn's value and nudges it; the Writer sees the transition, e.g. `Trust level: 25% -> 40% -> 5%`. Good for slow-moving stats — trust, tension, suspicion — anything that should creep rather than jump. The description is shown to the Writer alongside the value, so it knows what the number means.
- **Feedback** — points the other way: instead of steering the Writer, it produces a short out-of-character note shown to **you** after the reply. See [Feedback Fragments](feedback-fragments.md).
- **Direction note** — instead of shaping one reply, it records a lasting note that stays on the branch and keeps steering later turns. See [Direction Notes](direction-notes.md).

### Randomizing with `{{random}}`

Fragment text supports the inline [macros](macros.md); `{{pick}}` works everywhere `{{random}}` does. They resolve at different moments depending on where they live:

- **In a mood fragment's prompt text** — `{{random::crimson::azure}}` rolls **once per conversation** and the pick sticks, so a randomized style stays coherent across turns.
- **In a Director tool-call value** — if the Director itself writes `{{random::...}}` into a fragment value, it re-rolls **every turn it's emitted**.

To get the second behavior, tell the Director about the macro in the fragment's description — wrapped in backticks, e.g.:

> Pick the weather. To leave it to chance, output `{{random::clear::drizzle::storm}}` verbatim.

Macros inside single-backtick spans are **never resolved** (anywhere — descriptions, prompt text, messages), so the example survives to the Director's prompt as literal syntax it can copy. The un-backticked macro the Director emits then rolls fresh in the Scene Direction block each turn.

### Ordering

Drag the handle on any fragment row to reorder. Order matters twice: the Director fills fragments **top-down**, so an earlier fragment's answer informs the later ones (a one-way train of thought), and the Scene Direction block presents them to the Writer in the same order.
