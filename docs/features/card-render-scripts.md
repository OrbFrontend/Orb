# Card render scripts

Orb reads `extensions.regex_scripts` as two independent projections of stored
message content. Import, editing unrelated fields, and PNG export preserve the
original script declarations. Rendering and prompt construction never write
transformed text back to history.

- `promptOnly` scripts run on history after prompt macro substitution, before
  attachment annotations and speaker labels are added. System prompts and card
  definitions are outside this transform. The current solo user message is
  projected after macro resolution as well.
- `markdownOnly` scripts run before HTML rendering, sanitization, and cache
  lookup, including saved messages, streaming, and prose rewrites.
- Both flags, or neither, enable both channels. An unflagged script rewrites the
  stored row in the original engine, so its effect is visible to the reader and
  to the model alike; Orb reproduces that as two reads rather than a rewrite.
- `placement: [1]` targets user messages; `[2]` targets assistant messages.
  Disabled scripts are skipped. Scripts execute in declaration order.

In group history, each assistant message uses its own speaker's card. Group
user messages and unattributed summaries have no owning card, so no card script
applies. The off-turn workflow prefix uses the same projection as generation.

## Card editor

The Advanced tab lists imported declarations and provides a per-card enable
checkbox (`extensions.orb.card_scripts_enabled`, enabled unless explicitly
false). Disabling preserves the declarations. Changing scripts or enablement
reprojects historical messages and invalidates the model's cached prefix.

The explicit **Message stylesheet (CSS)** field lives at
`extensions.orb.display_css`. It applies to the character's assistant messages
through the existing message CSS sanitizer and per-message containment scope.
Creator notes remain prose; copy desired CSS into the stylesheet field to use
it. No new database column or migration is needed.

The character list exposes only `display_scripts` (find, replacement, placement)
and `display_css`, not the full extensions object. Card edits refresh these
values and rerender the chat.

## Supported subset and limits

A `/pattern/flags` literal is honoured when its flags are `g`, `i`, `m`, `s`, or
`u`. Anything else — a bare pattern, a missing closing delimiter, a repeated or
non-flag-shaped suffix — is treated as its own pattern with no flags, and so
replaces only its first match. A flag-shaped but unsupported literal (`/a/x`)
and a pattern that will not compile are skipped and logged.

Replacement tokens are `$0` and `{{match}}` for the whole match, `$1`–`$99` and
`$<name>` for groups, `$$`, `$&`, and the JavaScript prefix and suffix tokens.
An unknown group expands to nothing; a numbered group past the pattern's count
stays literal. Backslashes remain literal. Macros a replacement introduces are
resolved after expansion, so `{{char}}` in a `replaceString` reaches the model
and the reader as the name. Both channels run the same expansion; this is a
practical compatibility subset, not an exact port — the original engine leaves
`$$` and `$&` literal, and drops rather than keeps an out-of-range `$9`.

Python's `regex` library handles the prompt projection, with a 50 ms budget for
all scripts on one message. Timeout or oversized output restores the entire
original message. Execution is limited to the first 50 declarations, patterns
of at most 4,096 characters, and message inputs/outputs of at most 100,000
characters. These bounds affect execution, not stored declarations.

Display uses native JavaScript regular expressions and the same size/count
limits. Native browser regex has no timeout; pathological display patterns can
still stall rendering. Python and JavaScript regex engines are not identical, so
a pattern can still resolve differently between the two channels even though
both run the same flag parsing and replacement expansion.

Scripts come only from the character card. There is no user-level or
preset-level script list, so a group chat's user messages and unattributed
summaries — which have no owning card — are never projected. `placement` 3
(slash command), 5 (world info), and 6 (reasoning) are not implemented, and
neither are `minDepth`, `maxDepth`, `runOnEdit`, `substituteRegex`, or
`trimStrings`. Keyword lorebook scanning, round bookkeeping, autocomplete, and
summarization still read canonical text independently. Inline macros keep their
existing persistence-time behavior; history projection only substitutes prompt
identity macros. `{{trim}}` remains outside this feature.
