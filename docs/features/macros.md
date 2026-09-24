# Macros

A macro is a placeholder that Orb replaces with a value. Macros work in messages,
greetings, personas, scenarios, example messages, lorebook entries, fragments,
and saved state. They also work in the image-generation settings: style
prompts, extra instructions, character appearance prompts, and composition
skills.

| Macro | Result |
|---|---|
| `{{user}}` | Your name or the active persona's name |
| `{{char}}` | The character's name |
| `{{description}}` | The character's description, in full |
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

## `{{description}}`

`{{description}}` is the only macro that expands to a body of prose rather than
a name or a word. It resolves to the character's **Description** field -- the
field itself, not the Description and Personality that Orb joins together for
the prompt. In a group chat it is the speaking member's sheet, the same way
`{{char}}` is the speaking member's name.

Macros inside the description resolve after it lands, so a description reading
"{{char}} distrusts {{user}}" arrives with both names already filled in.

Two things to know before using it:

- **It is a prompt macro, not a display macro.** Written into a chat message it
  reaches the model expanded, but the message bubble still shows
  `{{description}}` -- the chat view does not hold the card's description.
- **A description is long, and length costs.** Dropping one into text that is
  already sent every turn pays for the same words twice, and anywhere a model
  is asked a narrow question about the current scene, prose that does not bear
  on the question measurably drags the answer toward the middle. A sentence you
  write yourself usually beats the whole field.

With no description to give -- a group scene with no sheet, a chat with no card
-- `{{description}}` is left alone rather than blanked, the same as `{{cast}}`
in a solo chat, so nothing silently disappears.

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

`{{user}}`, `{{char}}` and `{{description}}` always use the current values.
Random values use the location of the macro:

| Location | When it is chosen |
|---|---|
| Your message | When you send it |
| Character greeting | When you open a conversation, then frozen after your first message |
| Persona, scenario, or example message | Once per conversation |
| Mood-fragment prompt text | Once per conversation |
| An image-generation setting | Every render |
| A value written by the Director | Every turn |

`{{time}}` and `{{date}}` use the current value. In a message they freeze when
sent; in persona or scenario text they update each turn.

Checkpoints inherit the random values from their parent conversation.

## Macros in image generation

Image-generation settings are saved once and used in every chat, so `{{char}}`
there means the character of the conversation you generate from. This is how you
tell an image model which person a reference image shows:

```
<image1> is {{char}}.
```

Macros work in a style's positive and negative prompt, its extra instructions,
and in a composition skill's description and instructions. They also work in a
character's appearance and negative prompt under **This Character Only**, where
`{{char}}` always means that character -- in a group chat it is the member's
name, not the scene title.

A composition skill's **Name** is left alone, because Orb uses it to identify the
skill and shows it under the finished image.

The prompt shown in **Render details** is the finished text. Macros are already
resolved there, and **Reroll** sends exactly what that box says.

## Show a macro as text

Put a macro inside single backticks to prevent substitution:

```
Use `{{random::heads::tails}}` to flip a coin.
```

The backticks and macro remain in the text.
