# Persona Pinning

A persona describes you, the user. Pinning a persona keeps the right persona in a
conversation even when you change the global default.

## Pin scopes

Open the user menu with the **👤** button. Each persona can be pinned to:

- **This conversation**: affects only the open conversation.
- **This character**: becomes the persona for new conversations with that character.

The conversation option requires an open conversation. The character option
requires a saved character.

## Persona avatars

Edit a persona to give it a picture. **Choose image** opens the same crop editor
character avatars use; **Remove** drops back to the coloured circle holding the
persona's initial. The picture appears beside the persona in the user menu, and
in the chat gutter when avatars are turned on.

Turn the gutter on under **Settings -> Show avatars in chat**. It is off by
default. With it on, every message carries a portrait on the left: the speaking
character's for a reply, and the persona in force for your own messages -- so
switching or pinning a persona changes what your messages show.

## Which persona is used

Orb resolves the persona in this order:

1. Conversation pin
2. Character pin
3. Global default persona

The icon beside the active persona shows which level supplied it.

An existing unpinned conversation is pinned automatically when you send a message.
This keeps its author identity stable if you later change the global default. An
explicit unpin remains until the next send.

Selecting another persona while a conversation is pinned moves the pin to the new
persona. Deleting a persona removes pins that referred to it.
