# Anti-slop

Remove overused words, phrases, and the rhetorical patterns commonly seen in LLM outputs.

- The user maintains a Slop Phrase Bank of words and phrases they're allergic to.
- The final Editor pass checks the output for those phrases and prompts the Agent model to surgically rewrite only the sentences where they're found.

## Slop Phrase Bank

A user-editable list of banned words and phrases. Entries can be literal variants or regex patterns, and matching is fuzzy enough to catch close paraphrases while staying contained to a single sentence so rewrites are surgical.

## Contrastive negation ("Not X; but Y")

Detect the `Not X; but Y` rhetorical pattern (and its kin, like `isn't X, it's Y`) and ask for a rewrite. Only works within a single sentence boundary.

For repeated structure, sentence openers, and phrase reuse, see [Anti-repetition](anti-repetition.md).
