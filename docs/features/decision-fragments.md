# Decision Fragments

A decision fragment asks the **Judge**, a classifier model, one question about
the current situation, before or after the Director runs. The Judge selects an
outcome using the fragment's resolution policy. Orb sends that outcome's
guidance to the passes its **Inject** setting names, under **Major Decisions**.

Decision fragments run independently. Each sees the turn's situation, not another
fragment's result, so their order does not create a sequence. Orb batches questions
with the same rendered situation when limits allow; separate batches run at the
same time. If one question depends on another's answer, combine them into a single
choice or score decision.

## Create and configure

Create or edit an Interactive Fragment and set **Field Type** to **Decision**.
Orb includes a disabled **Outcome** seed that evaluates how the action in the
current request turns out.

In **Endpoints → Judge**, set the endpoint URL, API key, model, and optional
proxy. Orb derives the decisions route from the URL; a URL that already ends in
`/decisions` or `/systemone` is used as given. A llama.cpp server running a
decision model answers on its own `/v1/systemone` route, which Orb finds from the
server's base URL. Google AI Studio's decision model (`gemini-decision-1`) answers
at `/v1beta/decisions`, which Orb derives from the Google host, `/v1beta`, or the
`/v1beta/openai` chat URL; Orb sends that route Google's request field and API-key
header, and asks yes/no decisions as a choice between their two outcomes because
that route refuses outcome text on a yes/no question. **Test** sends a synthetic situation and does not
include conversation content. Without a configured Judge, decisions are skipped
and reported in the Inspector.

## Question types and resolutions

| Type | Judge answer | Available resolutions |
|---|---|---|
| **Yes/no** (`noul`) | Probability that the statement is true | **Threshold**, **Roll** |
| **Choice** | Probability for each named option | **Most likely**, **Random by odds**, **First option gates, else random** |
| **Score** | A score on a 2–10 level scale | **Most likely**, **Random by odds**, **Closest level** |

- **Threshold** resolves yes when its probability meets the configured cutoff.
- **Roll** resolves yes according to the probability.
- **Most likely** picks the option with the highest probability.
- **Random by odds** draws among the options according to their probabilities.
- **First option gates, else random** treats the first choice as a gate. If it is
  the most likely option, it resolves directly; otherwise Orb draws among the
  remaining options.
- **Closest level** maps the Judge's score to the nearest level.

Choice and score answers include a confidence value. Set **Min confidence** to
skip answers below a chosen value. Yes/no decisions do not use this setting.

For choice questions, give every option a distinct name: the Judge uses those
names in its answer. Option order breaks probability ties, and the gated
resolution always uses the first option as its gate.

Each outcome has two separate fields:

- **What it means** describes the outcome for the Judge to choose.
- **What the story does** is guidance injected for the passes **Inject** names.

Empty guidance means that outcome adds no instructions.

## Runs

**Runs** sets when the Judge asks the question:

| Runs | The Judge reads | Inject |
|---|---|---|
| **Before Director** (the default) | The conversation | Director, Writer or Both |
| **After Director** | The conversation and the Director's plan, as `{{scene_guidance}}` | Writer only |

An **After Director** decision judges what the Director just planned, for
example whether the attack it chose lands. The Director has already run, so
only the Writer receives the outcome. The Writer sees both the plan and the
outcome. The default prompt layout puts **Decisions** before **Scene guidance**,
so write guidance that overrides the plan ("Whatever the plan says, the lunge
misses."), or move the Decisions block below Scene guidance in the
[Prompt Layout](prompt-layout.md).

After Director decisions are asked only when the Director runs. Otherwise they
are skipped without asking the Judge, and the Inspector reports why. They
cost a second Judge request, between the Director and the Writer.

## Inject

**Inject** chooses which passes receive the outcome's guidance:

| Inject | Receives the guidance |
|---|---|
| **Director** | The Director and its before-Writer state updates |
| **Writer** | The Writer, at the top of its injection block |
| **Both** | Both of the above (the default) |

A decision always injects somewhere, so there is no Off setting. With
**Writer**, the Director plans the scene without knowing the outcome; with
**Director**, the Writer sees only the direction the Director wrote around it.

The Director reads decisions only when the Agent and the **Direction** tool are on.
Otherwise a **Director** decision is skipped without asking the Judge, and the
Inspector reports why.

## Situation and macros

The **Situation** template is the context sent to the Judge. It supports
`{{last_message}}`, `{{last_assistant_message}}`, `{{recent_history}}`,
`{{state}}`, `{{user}}`, `{{char}}`, `{{cast}}`, and, in solo chats,
`{{description}}` and `{{personality}}`. An **After Director** decision also has
`{{scene_guidance}}`: the Director's mood prompts and field values, as the
Writer's Scene Guidance carries them.

`{{state}}` lists every enabled [state fragment](state-fragments.md) that
holds something, under its injection label, whatever its **Inject** setting:
a tracker no pass is shown, such as hidden hit points, still reaches a
Situation that asks for it. Before the Director it is the state the turn
starts from, including your corrections. After the Director it also has the
turn's before-Writer changes, shown as `old -> new`. Changes made after the
reply land on the next turn.
`{{recent_history}}` contains up to the last six completed user and assistant
messages. To send a different number of messages, add a count from 1 to 50:
`{{recent_history::10}}`. The count is in messages, not exchanges. For
example, `{{recent_history::2}}` sends the last user message and the last reply.
The current request is available separately as `{{last_message}}`. Longer
history makes it more likely that the Situation exceeds the 16 KiB limit
described below.

Question instructions, outcome descriptions, and guidance support `{{user}}`,
`{{char}}`, and `{{cast}}`. A group decision from a character card uses that
character as `{{char}}` and that character's `{{description}}` and
`{{personality}}`, as [Macros](macros.md) describes for group chats. Macros
inside those fields resolve the same way they do for the Writer.

### Inline macros

You can use these macros in any decision field:

- `{{roll::1d20}}` rolls dice; `{{random::a::b}}` and `{{pick::a::b}}` choose
  one of the listed options.
- `{{time}}` and `{{date}}` insert the current local time and date.
- `{{trim}}` removes the surrounding line breaks. `{{// note }}` adds a note
  that Orb removes before sending the field to the Judge.

Orb expands inline macros before inserting chat text such as `{{last_message}}`
and `{{recent_history}}`. So text from a message is not evaluated a second time.

Regeneration keeps the same rolls for the same request; a new exchange gets
fresh rolls, except `pick`, which stays fixed for the conversation. All decisions in an exchange share the rolls in their Situation.
Rolls in instructions, criteria, and guidance are separate for each decision.
You can use `{{user}}`, `{{char}}`, and `{{cast}}` inside a `random` or
`pick` option. Situation values such as `{{last_message}}`, `{{recent_history}}`,
`{{description}}` and `{{personality}}` must stay outside those options.

`{{time}}` and `{{date}}` use the current clock, so they can change the Judge's
input between regenerations. For example, a Situation containing `{{time}}` may
change when the minute changes, preventing Orb from reusing the earlier answer.

The rendered situation limit is 16 KiB and the question limit is 8 KiB. An
oversized request is skipped; the Inspector shows its size and limit. A macro
with no value on the current turn also causes a skip. For resolved decisions,
the Inspector shows the situation sent and the Judge's answer distribution.

## Regeneration and group chats

When a regeneration has the same classifier input, Orb can reuse the stored
Judge answer. A changed or steered situation may need a new request. Draw-based
resolutions (`Roll`, `Random by odds`, and `First option gates, else random`)
draw again from the reused answer; the other resolutions keep their outcome.

An **After Director** decision's input includes the Director's plan, so a
regeneration whose Director plans differently asks the Judge again.

In a group chat, decisions run once per exchange and the result is shared by
each speaker's reply; **After Director** decisions read the exchange's shared
plan. Regenerating a later speaker keeps the exchange's committed result,
including its After Director outcomes, which the earlier speakers already wrote
from. If a member is given the floor after an unanswered user message, that
message becomes the decision's current request.

## Cooldowns and skipped decisions

A **Cooldown** rests a decision for that many completed turns, or group
exchanges. A cooldown starts when a decision resolves; skipped decisions do not
start one. A skipped decision contributes no outcome or guidance, while the rest
of the turn continues. The Inspector reports why it was skipped.

## Character cards

Character cards can include decision fragments. They run when that character is
in the scene, and the card editor validates them when saved. An invalid imported
definition is reported as skipped in the Inspector instead of being silently
ignored.

## Authoring tips

- Give decisions that run at the same time the same Situation template. Orb
  sends questions whose Situations render the same in one request, so they
  cost one Judge call instead of several.
- Make each fragment answer one question that is independent of the others.
  Combine dependent questions into one choice or score decision.
- Write criteria that distinguish the outcomes clearly. Keep the story
  instructions in **What the story does**.
- If a question does not apply on every turn, include a first option such as
  `no_attempt` with empty guidance and use **First option gates, else random**.
  This lets ordinary conversation resolve without rolling into a dramatic
  success or failure.
