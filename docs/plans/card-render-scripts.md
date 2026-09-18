# Card render scripts

Status: implemented (2026-09-18). See [the feature contract](../features/card-render-scripts.md)
for the shipped subset and limits. Phase 3 uses an explicit `extensions.orb.display_css`
field rather than executing creator notes. Secondary consumers remain deferred.
Literal patterns without `g` retain JavaScript's single-replacement behavior;
macro-order tests use identity substitution, because inline macros already run
at persistence time.

Revised 2026-09-18 after reading the original engine's source. Corrected: an
unflagged script now feeds both channels, not display only — see "Scope subset"
below; a pattern that is not a well-formed literal is its own pattern rather
than a global one or a dropped one; and both channels share one replacement
expansion covering `$0`, `{{match}}`, `$<name>`, and macros introduced by a
replacement. Still open, and deliberately not built:

- **One tier of three.** The original engine reads scripts from the user's own
  settings, from the card, and from the generation preset, in that order, and
  the card tier is the one it gates behind an explicit per-character opt-in.
  Orb has only the card tier, so no user can write a script, and a group chat's
  user messages have no owning card and are never projected. `CardScripts` holds
  a flat compiled tuple, so a second tier is a concatenation at the three
  construction sites plus a settings field and an editor.
- **Consent is inverted.** The original engine defaults card scripts off and
  prompts on first load, keyed by character in the *user's* settings. Orb
  defaults on and stores the decision in `extensions.orb`, so it exports with
  the card and a re-import re-grants it.
- **Placements 3, 5 and 6.** Reasoning (6) is the closest fit for Orb; world
  info (5) rewrites lorebook entry content at insertion.
- **Lorebook scanning is not parity, it is a divergence.** The original engine
  scans the prompt projection for keys (`chatForWI` is built from the regexed
  history), so the retarget under "Secondary consumers" is required for
  portability, not an incidental bug fix.

Support the card-supplied text transforms that SillyTavern calls "regex
scripts", so that cards built around a split between what the reader sees and
what the model reads work in Orb.

Orb has no **card-declared, read-time projection** of a
message body. Orb already renders card HTML and CSS well, and already runs two
independent read-time transforms over one stored body. It just has nothing
card-supplied plugged into either one.

## What the mechanism actually is

A reference card (`NIGHTMARE`, `chara_card_v3`, creator
`cognitohazard@riseup.net`) ships four scripts under
`data.extensions.regex_scripts`, all with `placement: [2]` (AI output). Two
flags split them into opposite channels:

| script | find | replace | flag | channel |
| --- | --- | --- | --- | --- |
| `dialogue` | `/<dialogue>/ig` | `<div class="dialogue-outer"><div class="dialogue">` | `markdownOnly` | display only |
| `dialogue2` | `/<\/dialogue>/ig` | `</div></div>` | `markdownOnly` | display only |
| `user-only` | `/<div id="user-only"(.*?)\n?<\/div>\n*?/igs` | *(empty)* | `promptOnly` | prompt only |
| `llm-only` | `/<div id="llm-only" .*?>(.*?)<\/div>/igs` | `$1` | `promptOnly` | prompt only |

This is not text munging. It is a two-view projection of one stored body, and
the card ships each greeting twice to exploit it:

- `<div id="user-only">` holds a styled artifact for the reader — in the
  default greeting, a 2005 fan page with a background image, an `<audio>`
  element, a marquee and period typography. It is **deleted before the prompt**.
- `<div id="llm-only" hidden>` holds the same content as plain markdown. It is
  **unwrapped into the prompt**, and the `hidden` attribute keeps it off screen.
- `<!-- ... -->` comments carry plot notes for the next several turns: model-only
  by nature, since a comment does not render.
- `<dialogue>` is a cheap semantic tag the model emits. The display turns it
  into a styled block; the prompt keeps the tag.

So the scripts do prompt-budget control and presentation at once, which is why
the card's creator notes say, in capitals, to keep regex on. Measured on this
card, the prompt-side scripts remove 40% of the default greeting and 67% of the
heaviest alternate greeting — text the model currently reads as content.

| greeting | stored | after prompt scripts | removed |
| --- | --- | --- | --- |
| `first_mes` | 5248 | 3123 | 40% |
| `alternate_greetings[1]` | 9785 | 3182 | 67% |
| `alternate_greetings[4]` | 6735 | 2794 | 59% |

## Current behavior, as measured

Verified against this repo: the card run through `backend/features/cards/parsing.py`,
and its text rendered through the real frontend pipeline under jsdom.

**Import already works.** All four scripts survive
`parse` → `card_to_dict` → `CharacterCardCreate` → the `extensions` TEXT column,
and come back out of `get_character_card`. Nothing in the repository reads them:
`regex_scripts` has zero references across backend, frontend, tests and docs.

**Display is largely correct already.** The fan-page greeting renders: background
image, audio element, marquee keyframes correctly scoped to the message, `hidden`
respected on the `llm-only` block, and the planning comment dropped rather than
leaked. `scopeClassName` ([`frontend/message_css.js:909`](../../frontend/message_css.js#L909))
prefixes `custom-`, which is the same convention the card's own CSS is written
against.

**One visible break.** `<dialogue>` is not a tag the browser knows, so
`escapeUnknownTags` ([`frontend/message_html.js:111`](../../frontend/message_html.js#L111))
escapes it, by design — dropping it would spill prose. Every reply from this card
therefore renders as literal text:

```text
&lt;dialogue&gt;Hm. That's funny.&lt;/dialogue&gt;
```

**The prompt path has no transform.**
[`backend/prompting/base.py:20`](../../backend/prompting/base.py#L20) passes stored
content through `{{user}}`/`{{char}}` substitution and nothing else, so the model
receives the full HTML and CSS block *and* the markdown duplicate of the same
text. Greetings are ordinary assistant message rows
([`backend/api/routes/conversations.py:202`](../../backend/api/routes/conversations.py#L202)),
so they travel this path like any other message.

## Design

Orb already has the shape this needs: one canonical stored body, two
independent read-time transforms.

```text
                 stored message content (canonical)
                    |                        |
     prompt read    |                        |   display read
                    v                        v
     macros.resolve_prompt            resolvePlaceholders
     prompting/base.py:20             utils.js:447
                    |                        |
          + promptOnly scripts      + markdownOnly scripts   <- new
                    |                        |
                    v                        v
               model payload            renderMessageHtml
```

`{{//}}` is already a prompt-side deletion
([`backend/core/macros.py:61`](../../backend/core/macros.py#L61)). Card scripts are
the same shape, only card-supplied. This is a new **input** to an existing seam,
not a new subsystem.

### Layering

`prompting/` may import `core/` and nothing else, so it cannot reach into
`features/cards/` to fetch a card. Mirror how `Macros` already solves this: a
`core`-level value type, constructed by a higher layer, passed down as an
argument.

- `backend/core/card_scripts.py` — a `CardScripts` NamedTuple beside `Macros`:
  compiled patterns plus `apply(text, channel)`. Dependency-free and pure.
- Constructed in `backend/pipeline/context.py` next to
  `Macros.from_settings` ([line 186](../../backend/pipeline/context.py#L186)), where
  the character card is already in scope.
- Threaded into `format_message_with_attachments(message, macros, scripts)`.

Export `CardScripts` from `backend/core/__init__.py` alongside `Macros`, and
classify nothing new in `scripts/check_backend_layers.py` — no new package.

### Translating the patterns

Scripts are authored as JavaScript literals and must be read as such:

- Parse `/pattern/flags`; map `i` → `re.I`, `s` → `re.S`, `m` → `re.M`. `g` is
  implied by `re.sub` and is not a compile flag.
- Rewrite `$1` … `$9` in the replacement to `\1` … `\9`, and `$$` to a literal `$`.
- Reject, and log, any pattern that fails to compile. One bad script must not
  take the card down.

Reproduce the semantics as written rather than improving them. The `user-only`
pattern is non-greedy and stops at the first `</div>`, so it would under-delete
a nested div; this card is authored around that and leaves zero markup residue.
A DOM-aware implementation that matched balanced tags would behave differently
on cards written for the original engine, which is the portability this feature
exists to provide.

### Scope subset

Honour `placement` 1 (user input) and 2 (AI output), `disabled`, `promptOnly`
and `markdownOnly`. Ignore `minDepth`, `maxDepth`, `runOnEdit`,
`substituteRegex` and `trimStrings` for now; nothing in the reference card uses
them, and each is a separate behavioral contract.

A script with neither `promptOnly` nor `markdownOnly` set rewrites the stored
body in the original engine, at write time: on user send, on reply receipt, on
greeting load, and on edit. Orb should **not** copy the rewrite — it is a
destructive edit of the canonical row, and it would bust the KV prefix for the
whole conversation on import. But because the rewrite lands in the row *both*
views read, its effect is visible in the prompt and on screen alike. The
faithful read-time mapping is therefore **both channels**, the same as a script
with both flags set; display-only would hide text from the reader and still feed
it to the model, which is backwards for the commonest script of this shape (one
that strips reasoning tags). Surface unflagged scripts in the card editor.

## Phases

### Phase 1 — prompt channel

Highest value, no UI, no new API surface.

1. `backend/core/card_scripts.py`: parse, compile, apply. Pure and unit-testable.
2. Build `CardScripts` in `backend/pipeline/context.py` from
   `card["extensions"]["regex_scripts"]`.
3. Apply in `format_message_with_attachments` after macro resolution, gated on
   role matching `placement`.

Order matters and should be asserted in a test: macros first, then scripts, so a
`{{//}}` comment inside a stripped block is gone either way and the byte result
does not depend on which ran first.

### Phase 2 — display channel

1. Add the scripts to the payload the chat view already holds. `GET /api/characters`
   ([`backend/database/queries/character_cards.py:42`](../../backend/database/queries/character_cards.py#L42))
   does not select `extensions`, and the chat never calls
   `GET /api/characters/{id}`, so `S.allCharacters` has no scripts today. Prefer
   adding a narrow, already-filtered `display_scripts` array to the list query
   over shipping the whole `extensions` blob to every card row.
2. `frontend/card_scripts.js`: the same parse-and-apply, in JS, where the
   patterns are already native.
3. Apply at the two render call sites in
   [`frontend/chat_stream.js:164`](../../frontend/chat_stream.js#L164) and
   [`:228`](../../frontend/chat_stream.js#L228), wrapping the existing
   `resolvePlaceholders` call.

Apply the transform **before** `renderMessageHtml`, not inside it.
`renderMessageHtml` memoizes on its `source` argument and derives the per-message
CSS scope from it ([`frontend/message_html.js:526-530`](../../frontend/message_html.js#L526-L530)).
Transforming inside would key the cache on pre-transform text, so a card edit
would serve stale HTML.

Once the transform runs first, `escapeUnknownTags` sees `<div class="dialogue">`
— a tag the browser knows — and the block survives the sanitiser, where
`scopeClassAttr` renames it to `custom-dialogue`.

### Phase 3 — card stylesheet

Phase 2 makes `<dialogue>` a real element, but nothing styles it. The card's
`.custom-dialogue` rules live in a `<style>` block inside `creator_notes`, which
Orb treats as plain text in the library editor
([`frontend/library.js:307`](../../frontend/library.js#L307)). The result is a
readable but unstyled block.

Orb has no card-level stylesheet concept. Per-message `<style>` already works and
is scoped ([`frontend/utils.js:416`](../../frontend/utils.js#L416) hands CSS to
`message_html.js` as an encoded `custom-style` element), so the work is to hoist
that machinery to a per-character sheet applied to every message from that
character, through the same scoping and the same policy in `message_css.js`.

Decide separately whether the source is `creator_notes` or a dedicated field.
Reading `creator_notes` matches the original engine — where the style leaks from
the notes panel — but it means a prose field is executable, which is worth
rejecting on its own terms.

## Risks and decisions

**Catastrophic backtracking.** Card patterns are attacker-supplied in the sense
that cards are downloaded from strangers, and Phase 1 runs them over full chat
history on every turn. Python's `re` has no timeout. The `regex` module does
(`regex.sub(..., timeout=)`) and is already present transitively, but is not a
declared dependency — adding it to `requirements.txt` is part of Phase 1. On
timeout, log and fall back to untransformed text. Also cap per-message input
length.

**KV cache.** The transform is a pure function of stored text, so it is
prefix-stable turn to turn, and it *shrinks* the prefix — a gain. But toggling
it, or editing a card's scripts, rewrites every historical message and busts the
prefix wholesale. Make it a stable card-level property, not a per-turn or
per-request toggle, and if a UI switch is added, warn that it re-reads history.

**Do not special-case the conventions.** `user-only` and `llm-only` are this
card's chosen ids, not a standard; another card will use different names.
Running the declared scripts *is* the generic mechanism. Hardcoding the two ids
would be precisely the hand-maintained per-card table to avoid.

**Trust boundary.** A card gaining influence over prompt text is a real
escalation — a script can delete an instruction it dislikes. It is bounded by
the fact that the card already authors that text, but the card editor should
show the scripts, and they should be disable-able per card.

## Secondary consumers

These read message content directly and bypass
`format_message_with_attachments`, so they keep seeing untransformed bodies after
Phase 1. None blocks the phases above; retarget bottom-up afterwards.

- [`backend/prompting/lorebook.py:124`](../../backend/prompting/lorebook.py#L124) —
  keyword scanning. Today a lorebook key can match inside a CSS URL or a class
  name, which is a live if minor correctness bug that Phase 1 fixes for free
  once this call site is retargeted.
- [`backend/pipeline/entrypoints.py:76`](../../backend/pipeline/entrypoints.py#L76)
  and [`:80`](../../backend/pipeline/entrypoints.py#L80) — `_round_prefix`, feeding
  the director and sheet passes.
- [`backend/features/autocomplete/service.py:51`](../../backend/features/autocomplete/service.py#L51) —
  autocomplete context.

## Tests

- Unit, `core/card_scripts.py`: flag translation, `$1` rewriting, an
  uncompilable pattern degrading to a no-op, `placement` gating by role,
  `disabled` honoured, timeout fallback.
- Unit, prompt path: the reference greeting in, the `llm-only` markdown out, no
  `<div` residue, and the macro/script ordering assertion.
- Frontend, `tests/frontend/`: `<dialogue>` surviving to `custom-dialogue`
  through the real sanitiser, in the existing jsdom harness
  (`message_html_render.test.mjs`); and the memoization check — same raw text,
  different card scripts, different HTML.
- Round trip: a card with scripts imported and exported unchanged. This holds
  today and must keep holding.

## Out of scope

`{{trim}}` is unsupported and leaks literally into the prompt from both
`description` and the depth prompt on this card. It is a one-line addition to
the macro table in [`backend/core/macros.py:92`](../../backend/core/macros.py#L92)
and unrelated to this plan; file it separately.
