# Decision Fragments

A decision fragment asks the **Judge**, a small classifier model, one question
about the scene before the Director runs. Orb resolves the answer to one of the
outcomes you wrote and sends that outcome's guidance to the Director and the
Writer under **Major Decisions**. The Director plans around the result; it does
not answer the question itself.

Create or edit an Interactive Fragment and choose **decision** as its Field
Type. Orb seeds a disabled **Outcome** decision that judges how the action in
your latest message turns out.

## Setup

Fill in the **Judge** section of the Endpoints settings: its URL, API key, and
model. **Test** sends a synthetic scene, never conversation content. Without a
configured Judge, every decision is skipped and no request is made.

## Question types

| Type | Answer | Resolutions |
|---|---|---|
| **Yes/no** (`noul`) | Probability that the statement is true | **Threshold** (true at or above a cutoff) or **Roll** (random by odds) |
| **Choice** | Probability for each option you name | **Most likely**, **Random by odds**, or **First option gates, else random** |
| **Score** | A 2–10 level scale | **Most likely**, **Random by odds**, or **Closest level** |

Choice and score answers carry a confidence. A **Min confidence** skips the
decision when the Judge is less sure than that.

Each outcome has its own guidance. Empty guidance injects nothing for that
outcome.

**First option gates, else random** is for a choice whose first option means
"this does not apply", such as the Outcome seed's `no_attempt`. This fixes the case where a trivial good morning rolls into crushing failure by sheer chance.

## The situation

The **Situation** is the text the Judge reads. It may use
`{{last_message}}`, `{{last_assistant_message}}`, `{{recent_history}}` (the last
six messages), `{{user}}`, `{{char}}`, `{{cast}}`, and, in solo chats only,
`{{description}}`. Questions, outcome descriptions, and guidance may use
`{{user}}`, `{{char}}`, and `{{cast}}`. In a group chat, a decision that came
from a character card reads that character as `{{char}}` and its scene sheet as
`{{description}}`, the same way the card's own text does. The editor preview shows the rendered
situation and its size; situations over 16 KiB are skipped.

Every decision in a turn sees the same situation, so one decision cannot see
another's result. Decisions that share a situation go out in one API request.

## Regeneration

Regenerating a reply reuses its stored answer without a new request. A
**Threshold** or **Most likely** outcome stays the same; a **Roll** or **Random
by odds** outcome is drawn again against the same odds. Magic Rewrite and
Super-regenerate get asked again.

In a group chat, decisions run once per exchange and every speaker receives the
same result. Regenerating, rewriting, or super-regenerating a later speaker keeps
the result the earlier speakers already wrote to, with no new request and no new
roll. Giving a member the floor after an unanswered message judges that message.

## Cooldowns and skips

A **Cooldown** rests a decision for that many turns after it resolves. A
decision that cannot answer (rested, timed out, or given an unusable answer) is
skipped: it contributes nothing and the turn continues.

## Character cards

Cards can carry decision fragments. Like a card's other fragments, they run
whenever that character is in the scene. The card editor checks a decision when
you save it. A broken one in an imported card is skipped every turn and reported
as invalid, so it never runs silently.
