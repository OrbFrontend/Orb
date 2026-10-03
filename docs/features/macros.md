# Macros

A macro fills in a name, roll, or other value before text reaches the model.
Use macros in messages, greetings, personas, scenarios, example dialogue,
lorebooks, fragments, saved state, and image-generation prompts.

## Examples

These examples use **Alex** as your name or active persona and **Mara** as the
character. Rolls, choices, dates, and times show possible results.

| Write | Example result |
|---|---|
| `Hello, {{user}}.` | `Hello, Alex.` |
| `{{char}} opens the door.` | `Mara opens the door.` |
| `Present: {{cast}}.` | `Present: Mara, Ivo.` (group chat) |
| `Luck: {{roll::2d6}}` | `Luck: 8` |
| `You find {{random::a key::a coin::nothing}}.` | `You find a coin.` |
| `Coin: {{pick::heads::tails}}` | `Coin: heads` |
| `Time: {{time}}` | `Time: 14:30` |
| `Date: {{date}}` | `Date: 2026-10-03` |

`2d6` rolls two six-sided dice and adds them. Use `NdM` for N dice with M sides.
`pick` works like `random`. Separate choices with `::`; choices can contain
spaces and line breaks, but not `::` or `}}`. Macro names ignore case.

In group chats, `{{char}}` uses the group title outside a member's card text;
`{{cast}}` lists the cast names.

## `{{description}}`

Inserts the full **Description** field, without **Personality**. In group chats,
it uses the speaking member's sheet.

If the description is `{{char}} distrusts {{user}}.`, then:

| Write | Sent to the model |
|---|---|
| `Background: {{description}}` | `Background: Mara distrusts Alex.` |

The chat bubble still shows `{{description}}`. If no description is available,
the macro stays unchanged. Avoid inserting the whole field where it is already
included in the prompt.

## Author notes and line breaks

`{{// ... }}` adds a note for the card's reader. The model never sees it:

```text
{{// Keep the secret out of the greeting. }}
{{char}} waves to {{user}}.
```

Sent to the model:

```text
Mara waves to Alex.
```

A note on its own line removes that line too. An inline note removes only itself.
Macros inside notes are removed without being evaluated.

`{{trim}}` removes line breaks around it, keeping spaces and tabs:

```text
Hello,
{{trim}}
 {{user}}.
```

Becomes `Hello, Alex.`

## When values are chosen

Names and descriptions use the current values. Rolls and random choices depend
on where you put them:

| Location | When it is chosen |
|---|---|
| Your message | When you send it |
| Character greeting | When you open a conversation, then frozen after your first message |
| Persona, scenario, example dialogue, or mood prompt | Once per conversation |
| Constant or keyword-activated lorebook entry | Once per conversation |
| Constant lorebook entry with **@ Depth** | Every turn |
| An image-generation setting | Every render |
| A value written by the Director | Every turn |

Time and date use the local clock. They freeze in sent messages but update each
turn in persona or scenario text. Checkpoints keep the parent's random values.

## Macros in image generation

Use macros in style prompts, negative prompts, extra instructions, character
appearance prompts, and composition-skill descriptions and instructions.

| Write in extra instructions | Example result |
|---|---|
| `<image1> is {{char}}. Keep her face consistent.` | `<image1> is Mara. Keep her face consistent.` |

Under **This Character Only**, `{{char}}` uses that character's name, including
in group chats. Composition-skill names stay unchanged.

**Render details** shows the resolved prompt. **Reroll** sends that same text.

## Show a macro as text

Wrap a macro in single backticks to keep it literal:

```
Use `{{random::heads::tails}}` to flip a coin.
```

This stays exactly as written, including the backticks.
