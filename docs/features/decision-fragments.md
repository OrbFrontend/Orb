# Decision Fragments

A decision fragment asks the **Judge**, a classifier model, one question about
the current situation, at its place among the Director fragments. The Judge
selects an outcome using the fragment's resolution policy. Orb sends that
outcome's guidance to the passes its **Inject** setting names, at its place in
the list.

Decisions next to each other, with no Director fragment between them, are
independent. Decisions asked at the same point read the same plan, and their
guidance appears in list order. Orb batches questions with the same rendered
situation when limits allow. Separate batches run at the same time. If one question depends on
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
Director**, and it runs top to bottom. Drag a decision to choose where its
outcome takes effect. A new decision starts above the Director fragments.

| Where the decision sits | `{{scene_guidance}}` reads | Inject **Director** reaches |
|---|---|---|
| Above every Director fragment | Nothing yet: a Situation that uses it is skipped | Every Director fragment, moods and before-Writer state |
| Between two Director fragments | The fields above it, and earlier rulings | The Director fragments below it, moods and before-Writer state |
| Below every Director fragment | The whole plan | Nothing: the decision is skipped |

Inject **Writer** reaches the Writer from any place. Below every Director
fragment, **Both** reaches the Writer only.

For example, place **Attack**, then “Does the attack land?”, then
**Consequence**. With **Inject Both**, the Director sees the Question while
planning the attack. If the Judge rules that the attack misses, the Director
plans the consequence with that ruling, and the Writer follows it. This order
holds even if the Judge could answer before the Director started.

Every Director request shows the Questions of eligible decisions still below
the fields it plans, including decisions below the last fragment. Each Question
says who follows the answer. The moods and one-value state step and the
before-Writer state update see the closing Questions too. A decision set to
**Writer** shows its Question throughout planning, wherever it sits, because
its answer never reaches the Director. A Question the Judge was asked but could
not answer, such as one that timed out, is not shown.

Between two fragments, a Situation that reads `{{scene_guidance}}` waits for
the fields and Director rulings above its place. At the close, a Situation
that reads `{{scene_guidance}}` or `{{state}}` waits for the finished plan and
the before-Writer state updates. Other
decisions can be asked at the start of the turn and share a Judge request when
their Situations match.

With **Individual fragment processing** off, a middle place adds one Director
call when a ruling there can guide the fragments below. If the Judge already
answered, only nonempty Director guidance causes a split. If the Judge needs
the plan to answer, the split costs one extra Director call plus a near-instant
Judge request. Decisions at the same place share the split. A Writer-only
decision can wait until the combined call returns and still judge exactly the
plan through its place, at the cost of the Judge request alone. Individual
processing already has a call boundary after each fragment.

A known skip, such as a resting decision, costs neither call. If the fragment
above a waiting **middle** decision gives no value, the Judge request is
skipped. A Director call already made to fill that fragment still counts.
Closing decisions read the finished plan even if the last fragment gave no value.

The Writer reads mood prompts first, then opening rulings, then Director values
and middle rulings, and finally closing rulings. Each group keeps list order.
A card's opening decision therefore comes before the global Director values,
and a global closing decision comes after the card's values.

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
| **Writer** | The Writer, in Scene guidance at the decision's place |
| **Both** | Both of the above (the default) |

Every decision has an Inject destination. With **Writer**, the Director can
plan around the Question while the answer
stays with the Writer. With **Director**, the Writer sees the direction the
Director wrote around the ruling. At a middle place with several rulings,
later Director steps and the Writer read their respective rulings in list order.

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
prompts, then the field values and the rulings that reached the Director, in
list order.

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
