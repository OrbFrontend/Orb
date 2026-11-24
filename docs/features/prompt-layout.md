# Prompt Layout

**Prompt Layout** in the sidebar sets the order of what the model reads in chat
mode. Drag blocks to reorder them, switch any block on or off, and add new text
blocks. Built-in blocks cannot be deleted, renamed, or moved across Chat
history.

Click a block to edit its text. A built-in block's window also says what it
carries and what produces it. The **Main prompt** holds your system prompt;
edit it there. It fills [macros](macros.md) such as `{{char}}` and `{{user}}`.
A character card's own system prompt replaces it unless **Prevent prompt
overrides** is on.

Character, Scenario, Example dialogue, Chat instructions and User persona are
ordinary blocks you can edit, switch off, delete, or move. Their text uses
placeholders that stand for the card and chat: `{{description}}`,
`{{personality}}`, `{{scenario}}`, `{{examples}}`, `{{instructions}}` and
`{{persona}}`. An empty placeholder is dropped with the blank line next to it,
and a block whose placeholders are all empty is left out.

Some built-in blocks hold text you can edit. Each names one placeholder that
must stay in the text, and the block is left out on turns where it is empty:

| Block | Placeholder | Stands for |
|---|---|---|
| Constant lore | `{{constant_lore}}` | Constant entries without **@ Depth** |
| Selected lore | `{{selected_lore}}` | Entries chosen this turn by keyword or by the Director |
| Depth lore | `{{depth_lore}}` | Constant entries with **@ Depth** |
| Decisions | `{{decisions}}` | The Judge's guidance |
| Scene guidance | `{{scene_guidance}}` | The Director's guidance |
| User message | `{{user_message}}` | Your message |

Group cast's text is fixed.

Switching a built-in block off keeps its text out of the prompt; whatever
produces it still runs. With the Director on and Scene guidance off, the
Director still runs and the Inspector still shows its guidance, but the Writer
never reads it. With Main prompt off there is no system prompt, the character
card's included. With User message off the model reads your message only from
the next reply on, as chat history; in a group, the later speakers of the same
round already read it there. Attached images still go.

Blocks above **Chat history** go to every pass, so changing them makes the model
re-read the whole chat once. A block there marked **clock** uses `{{time}}` or
`{{date}}`, so the model re-reads the chat whenever they change. Blocks below it
go only to the Writer and are read fresh every turn, so changing them costs
nothing extra.

Every block has a role and is sent where you put it. Neighbouring blocks with
the same role become one message. Some providers refuse some arrangements, such
as system text after the chat on Anthropic; the reply then fails with the
provider's reason. Assistant blocks at the very end start the reply, on
providers that allow it.

To use a different arrangement, create or copy a layout, or pick one from the
list. The selected layout is the one chat uses.
