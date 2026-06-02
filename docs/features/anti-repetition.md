# Anti-repetition

Detect and remove the various kinds of repetition LLMs fall into across a conversation. Each check runs in the final Editor pass and, on a hit, prompts the Agent model to rewrite the offending text.

- **Structural repetition**: LLMs tend to reuse the same paragraph structure across turns. Detect it and ask for an improved rewrite.
- **Template repetition**: Repeated mentions of a subject or repetitive sentence starters across paragraphs are detected and circumvented.
- **Repetitive sentence openers**: Get rid of back-to-back samey sentence openers like `He A. He B. He C. He D.`
- **Phrase repetition**: Catch the same distinctive phrase being reused across the reply (or recent turns) and ask for fresh wording.

For overused words and the `Not X; but Y` pattern, see [Anti-slop](anti-slop.md).
