# Anti-slop

Remove overused words, phrases, and patterns commonly seen in LLM outputs.

- The user maintains a Slop Phrase Bank that they're allergic to.
- The final Editor pass checks for phrases or words in the output and prompts the Agent model to surgically rewrite the sentences where the phrases are found.

Detect and remove various types of reptition and patterns.

- **Structural repetition**: LLMs tend to keep repeating the same paragraph structure across turns. Detect and ask for an improved rewrite.
- **Template repetition**: Repeated mentions of a subject or repetitive sentence starters (across paragraphs) can be detected and circumvented with this.
- **Repetitive sentence openers**: Get rid of back-to-back samey sentence openers like `He A. He B. He C. He D.`
- **Not X; but Y pattern**: Detect this pattern and ask for a rewrite. Only works within a single sentence boundary.
