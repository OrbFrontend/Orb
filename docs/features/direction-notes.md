# Direction Notes

A **direction note** is a kind of [Interactive Fragment](director.md#interactive-fragments) that records a lasting development from the story and carries it forward across the branch.

## Memory that persists

Ordinary interactive fragments shape a single reply and are recomputed each turn. [Feedback fragments](feedback-fragments.md) produce a one-off note for you. Direction notes are different again: each one is **written down and kept**, accumulating on the branch and staying in effect for every later turn until you remove it.

## Two switches: recording and injection

The feature has two halves, set independently in **Settings → Agents → Direction Notes**:

- **Recording** (the write side) — when on, the model is asked, in a separate out-of-character step, whether anything this turn is worth remembering, and writes a note per enabled direction-note fragment. Recording also requires the global Agent toggle and at least one enabled direction-note fragment.
- **Injection** (the read side) — feeds already-stored notes back into the prompt. Choose who sees them: **Off**, **Director**, **Writer**, or **Director and writer**. The Director sees them while it sets the scene (so it steers consistent with what it established before); the Writer sees them in its Scene Direction block.

The two are independent: you can inject stored notes without recording new ones, or record without injecting. Turning a note's authoring fragment off, or recording off, never hides notes that are already stored.

## When a note is recorded

Each direction-note fragment chooses its own timing, set by the **When recorded** selector in the fragment editor:

- **End of turn** (default) — runs after the reply is written, so the note can react to what actually happened.
- **Before writer** — runs after the Director sets the scene but before the Writer starts, so the note reflects the direction just chosen. This timing only fires when the Director's `direct_scene` step is active (it has nothing to reflect on otherwise).

A turn may run both — one fragment recording before the Writer, another at the end.

In a [group chat](group-chats.md) both placements are per **exchange**, not per speaker: the Director sets one direction for the whole exchange, so the before-writer step runs once beside it and its notes anchor to the exchange's first reply. The end-of-turn step runs once too, after the last speaker, where it can see how the exchange landed.

When the Director's per-fragment mode (each fragment filled in its own LLM call) is on, each direction-note category is recorded in its own call too, so the model's attention isn't split across categories.

## Notes follow the branch

A conversation is a tree: regenerating or editing forks a new branch. Direction notes are scoped to the **active branch**, not the whole conversation. A note recorded on a reply is in effect only while that reply is on the active path. Regenerate the reply and the new sibling starts without that note; switch back to the original branch and it returns. This keeps the Director's memory consistent with the storyline you're actually reading.

## Authoring direction-note fragments

Direction-note fragments are created like any other interactive fragment — same **ID**, **Label**, **Description**, and ordering — with the **field type** set to *direction note (persists)*. A couple of specifics:

- The **Description** is what the model reads when deciding whether to fill the note; word it as the *category* of thing to record.
- The **Injection label** becomes the note's heading wherever it's shown or injected.

Orb ships one direction-note fragment, **Characterization**, disabled by default.

## Adding your own notes

You don't have to leave every note to the model. While recording is on, each assistant reply grows a **note button** in its toolbar. It opens the **Notes panel** and a small dialog asking for a label and the note text; the note is stamped to that turn and lands on the branch exactly like a recorded one — branch-scoped, injectable, and editable.

Your own notes are marked with a **"You"** tag and an accent colour in the Notes panel (and in the Inspector's per-turn block), so they're easy to tell apart from the Director's.

The **Notes panel** itself (the right-rail **Notes** button, shown whenever recording or injection is on) lists every note on the current branch in turn order, each labelled with the fragment that wrote it and stamped with its turn, with edit and delete on each.

## Enabling it

1. In **Settings → Agents → Direction Notes**, turn on **Recording** (and/or pick an **Injection** target).
2. Enable at least one direction-note fragment — until recording is on, direction-note fragments are greyed out in the fragment list, the same way feedback fragments are gated.

!!! note "No extra cost on the prompt"
    Like the [feedback step](feedback-fragments.md), the recording step reuses the same cached prompt the Director/Writer/Editor already built and merely forces its `record_direction_note` tool, so it doesn't pay a fresh prompt-processing cost. See [KV Cache Reuse](../architecture/kv-cache.md).
