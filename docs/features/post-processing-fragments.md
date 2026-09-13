# Post-processing Fragments

A post-processing fragment is an Editor instruction that edits every generated
chat reply. It is useful for narrowly scoped finishing passes such as making
dialogue more natural, normalizing markup, or removing a recurring habit.

Create or edit an Interactive Fragment and choose **post-processing (edits
reply)** as its Field Type. The **Injection Label** becomes the Editor task
heading, and **Description** is the instruction the Editor follows. Required is
not applicable: every enabled post-processing fragment runs once.

The feature is active whenever the Agent and at least one post-processing
fragment are enabled. It does not depend on the Output Auditor or Length Guard.
Card-embedded fragments use the same fields and behavior.

## Pipeline placement

For each conversation reply, Orb runs post-processing:

1. after the Writer and any Output Auditor or Length Guard edits;
2. once per enabled fragment, in `sort_order`;
3. before Feedback and all secondary workflows.

Each fragment receives the Writer request and the current evolving draft. Its
edits therefore compose with earlier fragments. The post-processed text is the
retained `writer_draft`; a selected secondary workflow can still change the
visible reply afterward.

This placement applies to the shared conversation pipeline: send, continue,
regenerate, fork-edit, Magic Rewrite, and every generated group-chat reply.
Document Mode is unchanged.

## Exact-match safety

The Editor returns the built-in `editor_search_replace` tool:

```json
{"patches":[{"search":"exact current text","replace":"replacement text"}]}
```

Patches are applied sequentially to the evolving draft. A patch runs only when
`search` is a non-empty string with exactly one case-sensitive match in the
current draft and `replace` is a string different from it. Empty replacements
delete the uniquely matched span. Malformed, missing, ambiguous, empty, and
no-op patches are skipped while other valid patches still apply. Orb does not
retry a fragment.

The tool schema is frozen into the per-turn tool list whenever post-processing
is active. In the built-in order it follows `editor_rewrite` and precedes
`give_feedback`, preserving the Writer/Agent cache lanes.
