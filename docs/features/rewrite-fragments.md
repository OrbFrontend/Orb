# Rewrite Fragments

A rewrite fragment is an Editor instruction that rewrites every generated chat
reply in full. Use it for changes that touch most of the reply, where many
small exact patches would be fragile. For small, local changes, a
[recast fragment](recast-fragments.md) is cheaper and leaves the rest of the
reply untouched.

Orb ships one, **Trim**, disabled: it combines wordy phrases into fewer, more
impactful words and drops filler and loose beats. Enable it in the Interactive
Fragments list to try it.

Create or edit an Interactive Fragment and choose **rewrite (rewrites reply)**
as its Field Type. The **Injection Label** becomes the Editor task heading, and
**Description** is the instruction the Editor follows.

The feature is active whenever the Agent and at least one rewrite or recast
fragment are enabled. Card-embedded fragments use the same fields and behavior;
their `field_type` is `rewrite`.

## Pipeline placement

Rewrite and recast fragments run as one step, in a single `sort_order`, after
the Writer, any Output Auditor or Length Guard edits, and the subject fixation
edit, and before Feedback and all secondary workflows. Each fragment works on
the draft the earlier ones left, so a recast fragment placed after a rewrite
fragment patches the rewritten reply. The status line shows *Rewriting the
reply…* while a rewrite fragment runs.

See [Recast Fragments](recast-fragments.md#pipeline-placement) for which turns
this applies to.

## The rewrite

The Editor is forced to call the built-in `editor_rewrite` tool, the same one the
Length Guard and the structural-repetition audit use, with the whole new reply:

```json
{"rewritten_text":"the complete rewritten reply"}
```

Its text replaces the draft. The draft is kept unchanged when:

- the call is missing, or `rewritten_text` is missing, not a string, or blank;
- the response stopped at the token limit, since the rewrite may end mid-reply;
- the call fails, which is reported as *Rewrite "label" didn't finish.* while
  later fragments still run.

Orb does not retry a fragment. The tool is already in the per-turn tool list
whenever the Agent is on, so adding or toggling a rewrite fragment never
changes the cached tools.

## Gating

A rewrite fragment takes the same **Run only when** question and **History** as
a recast fragment, asked of the Judge about the current draft, and shares the
step's Judge budget. See [Recast Fragments: Gating](recast-fragments.md#gating).
Its Inspector entry is the same `fragment_gate` record, and card JSON uses the
same `gate_question` and `gate_replies` keys.
