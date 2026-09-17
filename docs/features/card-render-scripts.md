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
- Both flags enable both channels. Neither flag means display-only in Orb.
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

Patterns can be `/pattern/flags` literals or bare patterns (global by default).
Supported literal flags are `g`, `i`, `m`, `s`, and `u`; a literal without `g`
replaces only its first match. Replacement tokens include `$1`–`$99`, `$$`,
`$&`, and the JavaScript prefix/suffix tokens. Backslashes in replacements remain
literal. Invalid patterns are skipped and logged.

Python's `regex` library handles the prompt projection, with a 50 ms budget for
all scripts on one message. Timeout or oversized output restores the entire
original message. Execution is limited to the first 50 declarations, patterns
of at most 4,096 characters, and message inputs/outputs of at most 100,000
characters. These bounds affect execution, not stored declarations.

Display uses native JavaScript regular expressions and the same size/count
limits. Native browser regex has no timeout; pathological display patterns can
still stall rendering. Python and JavaScript regex engines are not identical;
this is a practical compatibility subset, not a complete ECMAScript emulator.

`minDepth`, `maxDepth`, `runOnEdit`, `substituteRegex`, and `trimStrings` are not
implemented. Keyword lorebook scanning, round bookkeeping, autocomplete, and
summarization still read canonical text independently. Inline macros keep their
existing persistence-time behavior; history projection only substitutes prompt
identity macros. `{{trim}}` remains outside this feature.
