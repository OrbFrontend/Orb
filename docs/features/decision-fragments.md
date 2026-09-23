# Decision Fragments

A decision fragment asks the **Judge**, a classifier model, one question about
the current situation before the Director runs. The Judge selects an outcome
using the fragment's resolution policy. Orb sends that outcome's guidance to
the Director and Writer under **Major Decisions**.

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
`/decisions` is used as given. **Test** sends a synthetic situation and does not
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
- **What the story does** is guidance injected for the Director and Writer.

Empty guidance means that outcome adds no instructions.

## Situation and macros

The **Situation** template is the context sent to the Judge. It supports
`{{last_message}}`, `{{last_assistant_message}}`, `{{recent_history}}`,
`{{user}}`, `{{char}}`, `{{cast}}`, and `{{description}}` in solo chats.
`{{recent_history}}` contains up to the last six completed user and assistant
messages. The current request is available separately as `{{last_message}}`.

Question instructions, outcome descriptions, and guidance support `{{user}}`,
`{{char}}`, and `{{cast}}`. A group decision from a character card uses that
character as `{{char}}` and its scene sheet as `{{description}}`. Macros inside
the description resolve the same way they do for the Writer.

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
fresh rolls. All decisions in an exchange share the rolls in their Situation.
Rolls in instructions, criteria, and guidance are separate for each decision.
You can use `{{user}}`, `{{char}}`, and `{{cast}}` inside a `random` or
`pick` option. Situation values such as `{{last_message}}`, `{{recent_history}}`,
and `{{description}}` must stay outside those options.

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

In a group chat, decisions run once per exchange and the result is shared by
each speaker's reply. Regenerating a later speaker keeps the exchange's committed
result. If a member is given the floor after an unanswered user message, that
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

- Make each fragment answer one question that is independent of the others.
  Combine dependent questions into one choice or score decision.
- Write criteria that distinguish the outcomes clearly. Keep the story
  instructions in **What the story does**.
- If a question does not apply on every turn, include a first option such as
  `no_attempt` with empty guidance and use **First option gates, else random**.
  This lets ordinary conversation resolve without rolling into a dramatic
  success or failure.
