# Prompt Layout

**Prompt Layout** in the sidebar sets the order of what the model reads in chat
mode. Drag blocks to reorder them, switch your own blocks on or off, and add new
text blocks.

Click any block to see its text. The **Main prompt** holds your system prompt;
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

Other built-in blocks show the text Orb sends, with the parts that change each
turn in brackets. Other settings switch them on and off, as the label on the
right says.

Blocks above **Chat history** go to every pass, so changing them makes the model
re-read the whole chat once. Each of them has a role: system blocks join the
system message, and user or assistant blocks become messages before the chat.
Blocks below it go only to the Writer, in one message, and are read fresh every
turn, so changing them costs nothing extra.

To use a different arrangement, create or copy a layout, edit it, and press
**Use this layout**.
