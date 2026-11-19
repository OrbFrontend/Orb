# Reviewing Changes

The assistant changes some things straight away and asks you before others.

**Waits for your review:**

- edits to the text of a character, persona, lore entry, message, document, or
  fragment;
- deleting a character, chat, message, world, lore entry, or banned phrase.

**Takes effect at once:**

- new characters, personas, worlds, lore entries, documents, fragments, and chats;
- switches: the Director, Output Auditor, Length Guard, Agentic Lorebook, and
  workflows;
- Length Guard limits, a lore entry's switches and keywords, a world's name and
  flags, a fragment's settings and order;
- the active persona, and a chat's title, locked persona, and worlds;
- copying a chat, showing another branch, adding a banned phrase;
- images and speech.

To reverse one of these, ask the assistant to set it back or change it yourself.
Clear and Edit undo nothing.

## The review card

An edit or delete stops the assistant and shows a card with what it would
change: a diff of the text with its length before and after, or for a delete
everything that goes with it. The full new text folds under the diff.

| Button | Does |
|---|---|
| **Apply & continue** (**Delete & continue**) | Commits the change; the assistant goes on with the rest of the request |
| **Skip & continue** | Leaves it out; the assistant goes on without it |
| **End request** | Leaves it out and stops |
| **Edit** | Opens the proposed text so you can change it before applying |
| **Undo** | After Apply, puts the old text back |
| **Apply all** / **Cancel all** | On a card with several changes |

To ask for a different version, type what you want instead of pressing a
button: "keep the second paragraph", "shorter". The assistant revises the
pending change.

A change you have not applied stays on its card while you keep talking, and the
assistant can build on it. Apply it within 15 minutes of its last change. Undo
works for 15 minutes after Apply. Orb forgets pending changes when it restarts,
and a reload leaves the card as a record without buttons; **Ask again** puts the
request back in the input.

When the text changed after the preview, Apply skips that change and the card
says so. When the chat is generating, the change stays pending; apply it once
the reply finishes.

## Deletes

A delete cannot be undone. The card says what goes with it:

| Deleting | Also removes |
|---|---|
| A character | Its chats, only when you ask for that: its solo chats and every group it is in |
| A chat | Its messages |
| A message | Its other versions and every message after it, on every branch |
| A world | Its entries |
| A banned phrase | Its variants |

The assistant offers a delete only when you ask for something to go. For a lore
entry you might want back, ask it to turn the entry off instead.

## Documents

In Document mode the assistant can create documents and propose edits to the one
you have open. Long documents change a passage at a time. An applied edit lands
in the editor as one undo step, and its text is marked as generated. If you
typed in the document after the preview, the editor's save-conflict choice
appears instead of replacing your text.

## Handing off to the chat

Some requests end with the chat doing the work, the same way its own buttons do:

| Ask for | Starts |
|---|---|
| A new version of a reply | **Regenerate** |
| A reply rewritten your way | **Magic Rewrite** with the assistant's direction |
| The story to go on | The next reply; in a group chat, from the character you name |
| Your next message written for you | **Send** with the assistant's text |
| A chat opened | The chat, as picking it in the chat list does |

It starts only while the chat is still the one open and nothing moved: the
reply is still on its branch, or the last message is unchanged. Otherwise the
log says why and nothing starts. From then on the chat's own **Stop** controls the
reply. On a narrow screen the panel closes so you can watch it.
