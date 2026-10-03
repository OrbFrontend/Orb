# Anti-slop

Anti-slop checks the Writer's reply for phrases and patterns you do not want.
When it finds one, the **Editor** asks the Agent to rewrite only the affected
sentences.

## Phrase bank

The **Slop Phrase Bank** is a list you maintain. Add words, phrases, or regular
expressions. Literal matching also catches close variants, while keeping each rewrite
within the sentence that contains the match. Python Regex is supported.

## Suggested phrases

The Phrase Bank's **Suggested** section lists phrases and sentence shapes that
the model writes far more often than people do, each as a ready-to-save regular
expression. A single "A beat." is ordinary English; the same line across dozens
of characters is a habit, and only the whole chat history shows it.

- **What is read:** model replies from every chat, swipes included. Your own
  messages are never read, and a chat's opening greeting is skipped because the
  card author wrote it. Each character counts once, so one long chat cannot
  carry a phrase.
- **The comparison:** replies are compared with text card authors wrote:
  greetings and example dialogue from your character library. A suggestion
  reads like `42 characters · 0 in card text, 9.6 expected`: if card authors
  wrote like the model, card text would hold the phrase about ten times, and it
  holds it none.
- **New** marks a phrase that has spread in the last 150 days. **Long-standing**
  marks a narration habit the model has had all along.
- Each suggestion also shows example sentences from different characters and
  the words that most often fill each `…` slot.

Choose **Add** to open the regular-expression editor with the pattern filled
in. Edit it if you like, then save it as a new group. Choose **Dismiss** to
remove a suggestion; it is never suggested again, and dismissals travel with
the Phrase Bank in presets and backups.

Nothing enters the Phrase Bank until you add it. A phrase that an enabled check
already catches for most of its examples is not suggested; the next one takes
its place.

Suggestions update in the background, in a separate process, when Orb starts
and after 200 replies are added or deleted, so they never slow a reply. They need replies
from at least 40 characters and enough card text to compare with.

## Built-in checks

- **Phrase bank** entries you add
- **Contrastive negation**, such as `Not X; but Y` or `isn't X, it's Y`
- **Anti-echo**, which catches a reply that repeats the user's quoted dialogue as
  an incredulous question

Anti-echo compares the assistant reply only with quoted dialogue in your previous
message. It ignores narration and `[OOC: ...]` notes, and short questions made
only of common function words do not trigger it.

The Editor applies these checks after the Writer finishes. You can review changes
when the editor diff is enabled in **Settings → Agents**.

For repeated structure, sentence openers, and phrase reuse, see
[Anti-repetition](anti-repetition.md).
