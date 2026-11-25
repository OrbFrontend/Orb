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
`random` and `pick` both choose one option; they differ in when the choice is
made (below). Separate choices with `::`; choices can contain spaces and line
breaks, but not `::` or `}}`. Macro names ignore case.

In group chats, `{{char}}` uses the group title outside a member's card text;
`{{cast}}` lists the cast names.

## `{{description}}` and `{{personality}}`

`{{description}}` inserts the card's **Description** field and `{{personality}}`
its **Personality** field, each on its own. In group chats they use the speaking
member's card; when that member has a scene sheet, `{{description}}` is the sheet
and `{{personality}}` is empty. In [Prompt Layout](prompt-layout.md) text they
are the card's sections instead, which are empty in a group.

If the description is `{{char}} distrusts {{user}}.`, then:

| Write | Sent to the model |
|---|---|
| `Background: {{description}}` | `Background: Mara distrusts Alex.` |

The chat bubble still shows the macro. If a field is empty, its macro sends
nothing. A field that names the other field fills it in; a field naming itself
sends nothing there. Avoid inserting a whole field where it is already included in the
prompt.

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

Names and descriptions use the current values. Each roll belongs to the place it
is written: a card field, the scenario, a prompt block, a lorebook entry. Two
places roll separately, even with the same macro, and so do two copies of a
macro in one place. Editing other text there keeps the result.

A value inserted elsewhere keeps its own rolls: `{{description}}` in the
scenario, a lorebook entry or a message reads the same text as the Character
block.

`{{pick}}` is chosen once per conversation for its place, wherever it is.
`{{random}}` and `{{roll}}` depend on the location:

| Location | When `random` and `roll` are chosen |
|---|---|
| Your message | When you send it |
| Character greeting | When you open a conversation, then frozen after your first message |
| Card fields, persona, scenario, example dialogue, chat instructions, mood prompt | Once per conversation |
| Prompt Layout blocks above **Chat history**, including the Main prompt | Once per conversation |
| Prompt Layout blocks below **Chat history** | Every turn |
| Constant or keyword-activated lorebook entry | Once per conversation |
| Constant lorebook entry with **@ Depth** | Every turn |
| An image-generation setting | Every render |
| A value written by the Director | Every turn |

Messages and greetings choose `pick` when they are written, like `random`.

Text above **Chat history** keeps its rolls so the model does not re-read the
whole chat each turn. **Reroll Macros** in the chat menu chooses every value the
conversation keeps fixed again, mood prompt choices included; the model re-reads
the chat once.

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
