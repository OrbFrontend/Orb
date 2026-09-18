# Macros

A macro is a placeholder that Orb replaces with a value. Macros work in messages,
greetings, personas, scenarios, example messages, lorebook entries, fragments,
and direction notes.

| Macro | Result |
|---|---|
| `{{user}}` | Your name or the active persona's name |
| `{{char}}` | The character's name |
| `{{roll::NdM}}` | The total from N dice with M sides, such as `{{roll::2d6}}` |
| `{{random::a::b::c}}` | One randomly selected option |
| `{{pick::a::b::c}}` | Alias for `{{random}}` |
| `{{time}}` | Local time in `HH:MM` format |
| `{{date}}` | Local date in `YYYY-MM-DD` format |
| `{{// note }}` | Nothing -- the text inside is a note to yourself |
| `{{trim}}` | Nothing, and removes the newlines surrounding it |

Random options are separated by `::`. Options may contain spaces and line breaks,
but not `::` or `}}`. Macro names are case-insensitive.

`{{// ... }}` is a note to yourself. Orb removes it before the character sees
any of it, so it is where reminders about how a card is meant to be played
belong -- the kind of thing you want the next reader of the card to see and the
model never to read.

A note alone on its line takes the whole line with it, so no blank line is left
behind; a note sitting mid-line takes only itself. Macros inside a note are
removed along with it rather than resolved, so `{{// ask {{user}} later }}`
disappears completely and rolls nothing.

`{{trim}}` joins the lines on either side of it, which is mainly how a card
drops the blank line a note would otherwise leave behind when something follows
it on the same line:

```
{{// Turn example messages on. }}{{trim}}

A demon of dark dreams...
```

The description reaches the model starting at `A demon`. Spaces and tabs around
the macro are kept -- only newlines go.

## When values are chosen

`{{user}}` and `{{char}}` always use the current names. Random values use the
location of the macro:

| Location | When it is chosen |
|---|---|
| Your message | When you send it |
| Character greeting | When you open a conversation, then frozen after your first message |
| Persona, scenario, or example message | Once per conversation |
| Mood-fragment prompt text | Once per conversation |
| A value written by the Director | Every turn |

`{{time}}` and `{{date}}` use the current value. In a message they freeze when
sent; in persona or scenario text they update each turn.

Checkpoints inherit the random values from their parent conversation.

## Show a macro as text

Put a macro inside single backticks to prevent substitution:

```
Use `{{random::heads::tails}}` to flip a coin.
```

The backticks and macro remain in the text.
