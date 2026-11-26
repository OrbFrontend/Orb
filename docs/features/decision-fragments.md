# Decision Fragments

A decision fragment asks the **Judge**, a classifier model, one question about
the current situation, at its place among the Director fragments. The Judge
selects an outcome using the fragment's resolution policy. Orb sends that
outcome's guidance to the passes its **Inject** setting names, under **Major
Decisions**.

Decisions next to each other, with no Director fragment between them, are
asked together and run independently: each sees the turn's situation, not
another's result. Orb batches questions with the same rendered situation when
limits allow; separate batches run at the same time. If one question depends on
another's answer, combine them into a single choice or score decision, or put a
Director fragment between them.

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

## Order

Decisions and Director fragments share one list in the Library, **Judge &
Director**, and it runs top to bottom. Drag a decision to choose when the Judge
asks it. A new decision starts above the Director fragments.

| Where the decision sits | `{{scene_guidance}}` reads | Inject **Director** reaches |
|---|---|---|
| Above every Director fragment | Nothing yet: a Situation that uses it is skipped | Every Director fragment, moods and before-Writer state |
| Between two Director fragments | The fields above it, and earlier rulings | The Director fragments below it, moods and before-Writer state |
| Below every Director fragment | The whole plan | Nothing: the decision is skipped |

Inject **Writer** reaches the Writer from any place. Below every Director
fragment, **Both** reaches the Writer only.

A decision between two Director fragments judges what the Director has decided
so far, and the fragments below build on its outcome. For example, put
**Attack** first, then a decision "Does the attack land?" whose Situation reads
`{{scene_guidance}}`, then **Consequence**. The Director plans the attack, the
Judge rules, and the Director writes the consequence knowing the attack missed.
The Director's request just before the decision shows its **Question**, so the
plan leaves room for whatever the Judge answers. Its `{{state}}` is still the
state the turn starts from.

A decision below every Director fragment judges the finished plan, after the
before-Writer state updates. The Writer sees both the plan and the outcome. The
default prompt layout puts **Decisions** before **Scene guidance**, so write
guidance that overrides the plan ("Whatever the plan says, the lunge misses."),
or move the Decisions block below Scene guidance in the
[Prompt Layout](prompt-layout.md).

A decision waits for its place only when its Situation reads something that
changes there: `{{scene_guidance}}` between Director fragments, and
`{{scene_guidance}}` or `{{state}}` below them. Any other decision is asked at
the start of the turn, in the same Judge request as the decisions above the
Director when their Situations match, and its outcome still reaches only what
its place reaches. The Director never stops for it. Each place where a decision
waits costs one Judge request, one after another.

If the Director fragment just above a waiting decision gives no value this
turn, because it rests or its request failed, the decision is skipped without
asking the Judge.

The list marks a decision its place makes skip with **!**.

A character card's decisions follow the card's own Director fragments in the
same way: above the card's first, between two, or below its last. A card with
only decisions asks them above the Director.

When the Agent or the **Direction** tool is off, every decision is asked at the
start of the turn. A decision whose Situation uses `{{scene_guidance}}`, or
whose Inject is **Director**, is skipped without asking the Judge, and the
Inspector reports why.

## Inject

**Inject** chooses which passes receive the outcome's guidance:

| Inject | Receives the guidance |
|---|---|
| **Director** | The Director steps after the decision, and its before-Writer state updates |
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
`{{state}}`, `{{scene_guidance}}`, `{{user}}`, `{{char}}`, `{{cast}}`, and, in
solo chats, `{{description}}` and `{{personality}}`. `{{scene_guidance}}` is the
Director's plan at the decision's place (see [Order](#order)): the field values
above it and earlier rulings, or, below every Director fragment, the mood
prompts and field values as the Writer's Scene Guidance carries them.

`{{state}}` lists every enabled [state fragment](state-fragments.md) that
holds something, under its injection label, whatever its **Inject** setting:
a tracker no pass is shown, such as hidden hit points, still reaches a
Situation that asks for it. Above or between Director fragments it is the
state the turn starts from, including your corrections. Below every Director
fragment it also has the turn's before-Writer changes, shown as `old -> new`.
Changes made after the reply land on the next turn.
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

A Situation that uses `{{scene_guidance}}` includes the Director's plan, so a
regeneration whose Director plans differently asks the Judge again.

In a group chat, decisions run once per exchange and the result is shared by
each speaker's reply; decisions after Director fragments read the exchange's
shared plan. Regenerating a later speaker keeps the exchange's committed
result, which the earlier speakers already wrote from. If a member is given the floor after an unanswered user message, that
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

- Give decisions that run at the same place the same Situation template. Orb
  sends questions whose Situations render the same in one request, so they
  cost one Judge call instead of several.
- Put `{{scene_guidance}}` last in a Situation between Director fragments. The
  plan grows from one place to the next, so the Judge's server can reuse the
  start of the previous request.
- Make each fragment answer one question that is independent of the others.
  Combine dependent questions into one choice or score decision.
- Write criteria that distinguish the outcomes clearly. Keep the story
  instructions in **What the story does**.
- If a question does not apply on every turn, include a first option such as
  `no_attempt` with empty guidance and use **First option gates, else random**.
  This lets ordinary conversation resolve without rolling into a dramatic
  success or failure.
