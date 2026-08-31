# Anti-slop

Anti-slop checks the Writer's reply for phrases and patterns you do not want.
When it finds one, the **Editor** asks the Agent to rewrite only the affected
sentences.

## Phrase bank

The **Slop Phrase Bank** is a list you maintain. Add words, phrases, or regular
expressions. Matching also catches close variants, while keeping each rewrite
within the sentence that contains the match.

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
