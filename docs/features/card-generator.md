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

**Tailored to me** has three modes. Your idea takes priority over whatever the
model learns from your library in either tailored mode, and an empty library
still works.

- **Off** drafts from your idea alone.
- **Library summary** adds a compact digest of your library.
- **Deep: reads your chats** lets the model research your conversations before
  drafting.

### Library summary

The digest holds your card count, tag vocabulary and common tags, up to twenty
persona names (most-used first), and up to five characters with the most
conversations. It reads no character descriptions, persona descriptions, or
conversation messages, and adds no model calls. It works with both chat and
text-completion endpoints.

### Deep tailored

> **Privacy:** Deep mode sends excerpts of your chats, characters, and persona
> descriptions to your Agent endpoint. Use a provider you trust with that data.
> The panel shows this warning whenever Deep is selected; it does not block
> generation.

Deep mode starts from the same digest, then runs a short research loop. At each
step the model writes one read-only SQL query over your library, reads the
result, and decides whether it has learned enough about your themes, tone,
relationship dynamics, pacing, and writing. It then drafts the card from
everything it read. Thinking is always on in Deep mode, so the **Enable
generator thinking** checkbox is locked on while Deep is selected; your own
choice returns when you pick another mode. Deep mode is slower and uses more
tokens than the other modes.

The model can read four views, and only these columns:

| View | Columns |
|---|---|
| `conversations` | `id`, `title`, `character_card_id`, `character_name`, `persona_id`, `kind`, `created_at`, `updated_at`, `last_accessed_at`, `active_leaf_id` |
| `messages` | `id`, `conversation_id`, `role`, `content`, `parent_id`, `turn_index`, `created_at` |
| `characters` | `id`, `name`, `description`, `personality`, `scenario`, `first_mes`, `mes_example`, `creator_notes`, `tags`, `alternate_greetings`, `creator`, `source_format`, `created_at`, `updated_at` |
| `user_personas` | `id`, `name`, `description`, `created_at`, `updated_at` |

`conversations.persona_id` is the persona each conversation speaks as today:
the conversation's persona lock, then its character's lock, then the active
persona. Everything else is hidden: avatars, system prompts and post-history
instructions, card extensions, workflow state, writer drafts, tagging and
duplicate-finder bookkeeping, settings, endpoints and API keys, and every other
table.

Queries cannot change anything. The database is opened read-only, and before a
statement runs SQLite refuses it unless it is a single `SELECT` that reads only
the columns above. That rules out every other column and table, schema reads,
`PRAGMA`, `ATTACH`, and extension loading. Each query stops after 5 seconds. A
result holds at most 50 rows, cuts text longer than 500 characters, and drops
trailing rows past about 4,000 characters. SQL errors and refusals go back to
the model as the result, so it can correct its query.

Research ends after at most 25 steps, or sooner when the model reports it has
finished, sends an empty query, or replies without a query. The panel shows
one progress label per query, such as *Researching your library: most-played
characters (step 2 of 25)…*, then *Drafting your character…*. A provider error
on the first step is reported like any other.

A reply cut off at **Max Tokens**, or one that cannot be read as a query, does
not count as finishing. The step is retried once per run, and the panel says
why, as in *Research step 4 failed. The model's reply was cut off at the Agent
Max Tokens limit of 4096. Retrying…*. If the first step fails again, the error
appears in the panel. On a later step, a provider error or a second broken reply
ends research, and drafting continues with what was learned under a label that
says why, such as *Research stopped at step 4. The model's reply was not a
readable query. Drafting from 3 queries…*.

The model sees whatever went wrong on its next pass. A failed query comes back
as that step's result. A drafted card that breaks the rules under
[Model and output](#model-and-output) is shown to the model with the reason, and
it redrafts once, as in every mode; in Deep mode the panel shows *Fixing the
draft…*. If drafting still fails, or the endpoint returns no card (for example,
the transcript no longer fits the model's context), the generator retries once
from a compact prompt: your idea, the digest, the model's research notes, and
the latest rejection reason. The panel shows *Drafting from research notes…* for
that retry. Drafting stops after three attempts, and the last error appears in
the panel.

On text-completion endpoints each research step is grammar-constrained, so the
model cannot think before choosing a query. Each step's `findings` field holds
its notes on the previous result instead, and those notes feed the compact
retry. Small context windows are more likely to need that retry.

## Model and output

Generation uses the configured Agent lane, falling back to the Writer lane
when they share an endpoint. **Enable generator thinking** is off by default;
turning it on takes longer and uses more tokens. Every call uses the Agent
model's configured **Max Tokens** (the Writer's **Max Tokens** when no separate
Agent lane is set up), and thinking is spent from that same budget. A reply cut
off at that limit fails with a message naming the setting to raise; it is never
shown to the model as a rule violation.

Long prose is trimmed at a sentence boundary where possible, then a word
boundary. Paragraph breaks and the native `{{char}}` / `{{user}}` macros are
preserved. Other braces are rejected, as are missing names, names over 100
characters, and missing opening messages. Names and creator notes permit no
macros. A rejected draft is shown to the model with the reason, and it redrafts
once in the same request; if that draft is rejected too, or the endpoint returns
no card, the error appears in the panel. Review the draft before saving,
especially after a long idea.

Saved cards carry `source_format: "generated"`. The streaming endpoint is
`POST /api/library/card-generator/run` with `idea`, optional `reasoning`, and
optional `tailoring`: `off` (the default), `summary`, or `deep`. `deep` implies
thinking. The stream emits `start`, a `progress` label for each
phase (reading library preferences when tailored, each research step in Deep
mode, then drafting), then `done` with an unsaved `card`, or `error` with a
plain-text sentence.
