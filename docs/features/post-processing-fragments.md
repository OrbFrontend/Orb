# Post-processing Fragments

A post-processing fragment is an Editor instruction that edits every generated
chat reply. It is useful for narrowly scoped touch-up passes and enforcing
the Director's will if the Writer missed something.

Create or edit an Interactive Fragment and choose **post-processing (edits
reply)** as its Field Type. The **Injection Label** becomes the Editor task
heading, and **Description** is the instruction the Editor follows.

The feature is active whenever the Agent and at least one post-processing
fragment are enabled. It does not depend on the Output Auditor or Length Guard.
Card-embedded fragments use the same fields and behavior.

## Pipeline placement

For each conversation reply, Orb runs post-processing:

1. after the Writer, any Output Auditor or Length Guard edits, and the subject
   fixation edit;
2. once per enabled fragment, in `sort_order`;
3. before Feedback and all secondary workflows.

Each fragment receives the Writer request and the current evolving draft. Its
edits therefore compose with earlier fragments. The post-processed text is the
retained `writer_draft`; a selected secondary workflow can still change the
visible reply afterward.

A fragment's gate, when it has one, is asked just before that fragment runs;
see [Gating](#gating).

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

The tool schema is frozen into the per-turn tool list whenever the Agent is on,
so defining or toggling a fragment never rewrites it. In the built-in order it
follows `editor_rewrite` and precedes `give_feedback`, preserving the
Writer/Agent cache lanes.

## Gating

A fragment can have a **Run only when** question, such as *"Do more than two
distinct actions happen in the reply?"*. Before the fragment runs, Orb asks the
**Judge** that question about the current draft. A yes runs the fragment; a no
skips it without an Editor call, so the draft and the cached prefix are
untouched. An empty question means the fragment runs every turn.

Gating needs a Judge endpoint in **Endpoints → Judge**, the same one decision
fragments use.

**What the Judge sees.** The user's message, the draft, and as many earlier
replies as the fragment's **History** asks for (0 to 10, default 0):

```text
Previous reply:
<an earlier assistant reply, one block per History reply>

Current request:
<the user's message for this turn>

Reply:
<the draft as it stands>
```

With History at 0 the Judge sees nothing of the chat before this turn, so it
can only answer questions about the reply itself. Raise History when the answer
depends on what came before, such as whether a character could know something.
Start at 1 and go higher if the gate misses things that happened further back.
Earlier user messages are not included.

The draft is the evolving one: after Output Auditor, Length Guard, and subject
fixation edits and after every earlier post-processing fragment. Each gate is
judged on its own draft, one at a time. In a group chat, every generated reply
has its own gates.

The question is sent as a yes/no (`noul`) question with fixed criteria: *"The
answer to the question is yes based on the reply."* and its no counterpart. The
gate text gets no macro expansion, the same as the Description.

**Cutoff.** A probability of 0.5 or more is yes, so exactly 0.5 runs the
fragment. The cutoff is not configurable.

**Fail-open.** Anything that keeps the Judge from answering runs the fragment,
as if it had no gate:

| Reason | When |
|---|---|
| `not_configured` | No Judge endpoint or model is set |
| `oversized_input` | The previous replies, request and draft together are over 16 KiB, or the question plus criteria is over 8 KiB (UTF-8 bytes) |
| `budget_exhausted` | The turn's gates have used up their Judge-wait budget |
| `timeout` | The Judge did not answer within the remaining budget |
| `transport_failure` | The Judge could not be reached or returned an error |
| `invalid_answer` | The Judge's answer was missing or unusable |

The authoring form caps a question at 2,000 characters. Card-embedded questions
skip that cap, so the byte limits above are checked when the gate runs; an
oversized question is never truncated.

**Budget.** All gates in one post-processing step share six seconds of Judge
waiting. Editor calls do not count against it. Once the budget is spent, the
remaining gated fragments run without asking.

**Stop.** Stopping the turn cancels a pending gate request. Edits made before
the Stop are kept, and no later fragment, Feedback, or workflow starts.

### In the Inspector

Each gate adds a `post_processing_gate` entry under **Tool Calls**, before its
fragment's `editor_search_replace` call when that runs:

```json
{"fragment_id":"trim","label":"Trim","question":"Do more than two distinct actions happen in the reply?",
 "fired":0,"reason":"condition_not_met","probability":0.08}
```

`fired` is 1 when the fragment ran. A Judge verdict has reason
`condition_met` or `condition_not_met` and carries its `probability`. A gate
with History lists how many replies it sent as `previous_replies`. Any other
reason is a fail-open from the table above and has no probability; an
`oversized_input` entry also lists the byte sizes and limits.

### Wording check

The fixed criteria were chosen by asking the configured Judge
(`typesafe/jev-1.13`) *"Do more than two distinct physical actions happen in the
reply?"* about fixed drafts, twice each:

| Draft | Bare "Yes." / "No." | Fixed criteria above |
|---|---|---|
| One action ("Mara sat down at the corner table.") | 0.08 | 0.08–0.10 |
| One action, request asking for many | 0.07–0.08 | 0.04 |
| Two actions, no speech | 0.11 | 0.09 |
| Two actions plus speech | 0.48–0.50 | 0.36–0.46 |
| Five actions (two drafts) | 0.97–0.98 | 0.97–0.98 |

Both wordings put the clear cases on the right side of 0.5, and the request did
not sway the verdict. The bare wording put the borderline two-action draft on
the cutoff (0.50 fires); the fixed criteria kept a clear margin. A one-action
draft that also has speech and a glance scored 0.54–0.59 under both wordings.
Questions about counts should name what counts, such as "physical actions, not
speech".
