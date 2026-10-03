# Anti-slop

Anti-slop checks the Writer's reply for phrases and patterns you do not want.
When it finds one, the **Editor** asks the Agent to rewrite only the affected
sentences.

## Phrase bank

The **Slop Phrase Bank** is a list you maintain. Add words, phrases, or regular
expressions. Literal matching also catches close variants, while keeping each rewrite
within the sentence that contains the match. Python Regex is supported.

## Suggested phrases

Open **Slop Phrase Bank → Suggested** to find phrases and sentence patterns
the model overuses across your chats. Orb compares model replies, including
swipes, with character greetings and example dialogue. It does not use your
messages.

- **Add** opens a ready-made regular expression. Review or edit it, then save
  it to the Phrase Bank. Suggestions do not block anything until you add them.
- **Dismiss** hides the suggestion permanently. Presets and backups keep your
  dismissals.

Each suggestion shows examples. In sentence patterns, `…` marks words that can
vary; the common choices appear below the examples.

For example, `42 characters · 0 in card text, 9.6 expected` means the pattern
appears in replies for 42 characters but never in card text. At the model's
rate of use, it would appear about ten times in that card text.

**New** means use has increased in the last 150 days. **Long-standing** means
an older, recurring habit.

Suggestions update automatically in the background. They need replies from at
least 40 characters and enough card text for comparison.

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
