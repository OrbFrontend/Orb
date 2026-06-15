# Feedback Fragments

A **feedback fragment** is a kind of [Interactive Fragment](director.md#interactive-fragments) that, instead of steering the Writer, produces a short out-of-character note **for you** after the reply is written.

## The inverted direction

Ordinary interactive fragments point *inward*: the Director fills them in to shape what the Writer produces (AI → AI). Feedback fragments point *outward*: once the reply is finished, the model steps out of character and writes a note to the player (AI → user). The note is shown in the **Inspector** panel and never reaches the Writer or affects the story.

Think of it as a game master leaning over to give you a tip between turns.

## What it's good for

Anything you want the model to tell *you* rather than weave into the prose:

- **Suggestions** — "2 fresh things you could do next" (this ships as a built-in, disabled by default).
- **Coaching** — pacing notes, reminders of dangling threads, tone observations.
- **Meta status** — anything that would break immersion if it appeared in-scene.

## How it differs from other fragments

Feedback fragments are authored exactly like any other interactive fragment — same **ID**, **Label**, **Description**, **Required** flag, and ordering — but with the **field type** set to *feedback (note to you)*. The differences:

- They run in a separate **post-writer** step, after the reply (and any [editor](anti-slop.md) edits) is final, so the note can react to the actual finished text.
- Their **Injection label** is used as the heading for the note in the Inspector.
- They're collected together into a single `give_feedback` call — you can have several, and each becomes one row in the feedback card.

## Enabling it

Feedback fragments are gated behind the **Editor Feedback** feature flag in **Settings → Agents**. Until you turn that on, any `feedback`-type fragments are greyed out in the fragment list and won't run. Enable the flag, then enable the individual feedback fragments you want.

!!! note "No extra cost on the prompt"
    The feedback step reuses the same cached prompt the Writer and Editor already built and simply forces the `give_feedback` tool at the end, so it doesn't pay a fresh prompt-processing cost. See [KV Cache Reuse](../architecture/kv-cache.md) for the caching design.
