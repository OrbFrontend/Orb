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

Random options are separated by `::`. Options may contain spaces and line breaks,
but not `::` or `}}`. Macro names are case-insensitive.

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
