# Card Generator

Open **Character Library → Manager → Card Generator**, describe a character,
and press **Generate**. The Agent model drafts a card and opens the character
editor. Review any field, optionally add an avatar, and press **Save** to add it
to your library. Saving or cancelling the editor returns you to Manager.
Nothing is saved until you approve it in the editor.

Ideas can contain up to 2,000 characters. The draft includes a name, description,
personality, scenario, opening message, example dialogue, and creator notes.
It has no avatar or prompt overrides. Use Auto-tagging to assign library tags
after saving; generated cards are eligible like any other new card.

**Cancel**, closing the library, or leaving Manager stops the current request.
Provider errors appear in the panel; edit your idea or retry when ready.

## Tailored to me

This optional checkbox uses a compact digest of your library: card count,
tag vocabulary and common tags, up to twenty persona names (most-used first),
and up to five characters with the most conversations. It reads no character
descriptions, persona descriptions, or conversation messages. Your idea takes
priority over these preferences. An empty library still works.

Tailoring works with both chat and text-completion endpoints. It adds no
reconnaissance model calls; a database tool loop is deferred until the digest
proves insufficient.

## Model and output

Generation uses the configured Agent lane, falling back to the Writer lane
when they share an endpoint. **Enable generator thinking** is off by default;
turning it on takes longer and uses more tokens. The minimum reply allowance
is 4,096 tokens, or 8,192 with thinking. A larger configured budget is preserved.

Long prose is trimmed at a sentence boundary where possible, then a word
boundary. Paragraph breaks and the native `{{char}}` / `{{user}}` macros are
preserved. Other braces are rejected, as are missing names, names over 100
characters, and missing opening messages. Names and creator notes permit no
macros. Review the draft before saving, especially after a long idea.

Saved cards carry `source_format: "generated"`. The streaming endpoint is
`POST /api/library/card-generator/run` with `idea`, optional `reasoning`, and
optional `tailored`. It emits `start`, a `progress` label for each phase
(reading library preferences when tailored, then drafting), then `done` with an
unsaved `card`, or `error` with a plain-text sentence.
