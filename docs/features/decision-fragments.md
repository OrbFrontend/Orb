# Decision Fragments

A decision fragment asks the **Judge**, a classifier model, one question about
the current situation before the Director runs. The Judge selects an outcome
using the fragment's resolution policy. Orb sends that outcome's guidance to
the Director and Writer under **Major Decisions**.

Decisions are independent because of their nature. Each sees the same turn snapshot;
one decision cannot read another's result, and fragment order does not create a 
dependency. Questions with the same rendered situation are batched where limits allow;
separate batches run concurrently. If one result should affect another question, 
combine them into one choice or score decision.

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
`{{char}}`, and `{{cast}}`. In group chats, a card decision uses that card's
character as `{{char}}` and its scene sheet as `{{description}}`. The
description's own macros resolve as they do for the Writer.

Every field also supports the inline macros: `{{roll::1d20}}`,
`{{random::a::b}}`/`{{pick::a::b}}`, `{{time}}`, `{{date}}`, `{{trim}}`, and
`{{// comments}}`. They resolve in the template before the snapshot's values
are inserted, so a macro typed inside a message is never evaluated. Rolls are
fixed for an exchange: a regeneration sees the same values and can reuse the
Judge's answer, and the next exchange rolls again. The Situation rolls once per
exchange, so every decision sees the same world and equal templates still share
a request; the other fields roll separately for each decision. `{{user}}`,
`{{char}}`, and `{{cast}}` may sit inside `{{random}}` or `{{pick}}`; the
situation macros may not. `{{time}}` in the Situation changes the request every
minute, so it can stop a regeneration from reusing the answer.

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
