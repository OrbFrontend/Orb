# Card render scripts

Orb supports card-supplied regular-expression scripts for projecting message
text at read time. The stored message remains canonical: scripts affect the
text sent to the model and/or the text rendered in chat, but do not rewrite
history.

Scripts are read from the card's `extensions.regex_scripts` array. They run in
declaration order. A card can disable all of its scripts with
`extensions.orb.card_scripts_enabled: false`; scripts are enabled by default.

## Prompt and display projections

Each script can target user messages (`placement: [1]`), assistant messages
(`placement: [2]`), or both. Its scope is controlled by these flags:

- `promptOnly: true` applies the script to the model prompt.
- `markdownOnly: true` applies the script to chat display text.
- With neither flag, or with both flags, the script applies to both.

The prompt projection resolves identity macros in the message first, then
runs prompt scripts before attachment annotations and group speaker labels are
added. Display scripts run before HTML rendering and sanitization. Replacement
text can introduce identity macros; those macros are resolved after the
replacement is expanded.

In a group conversation, an assistant history message uses the scripts from
the card belonging to its recorded speaker. The same prompt projection is
used for generation and off-turn workflow prefixes.

Scripts are projections only. PNG import/export and card editing preserve the
original declarations.

## Card editor

The card editor's Card rendering section provides:

- an enable/disable switch for the card's scripts;
- editable script name, search pattern, replacement text, message targets, and
  prompt/display scope;
- per-script enablement and controls to add, remove, and reorder scripts; and
- an explicit **Message stylesheet (CSS)** field.

Saving the card applies the edited declarations. The stylesheet is applied to
assistant messages, then sanitized and scoped to the individual message. It
accepts CSS directly or CSS pasted inside `<style>` blocks.

## Patterns and replacements

Use JavaScript-style patterns such as `/pattern/gi`. Orb supports the `g`,
`i`, `m`, `s`, and `u` flags. `g` replaces every match; without `g`, only the
first match is replaced. A pattern without a usable literal flag set is
treated as the complete pattern text and uses first-match behavior.

Replacement text supports:

- `$0` and `{{match}}` for the complete match;
- `$1`–`$99` for numbered capture groups;
- `$<name>` for named capture groups;
- `$$` for a literal dollar sign;
- `$&` for the match, plus the JavaScript prefix and suffix replacement tokens.

Backslashes in replacement text remain literal. Unknown named groups expand to
an empty string; an out-of-range numbered group remains literal.

Invalid or disabled entries are skipped without preventing the rest of the
card from loading.

## Limits

Orb evaluates at most the first 50 script declarations. A search pattern may
be at most 4,096 characters, and a message projection is limited to 100,000
characters. If prompt execution times out or a projection exceeds the output
limit, Orb keeps the original message text for that projection.
