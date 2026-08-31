# Persona Pinning

A persona describes you, the user. Pinning a persona keeps the right persona in a
conversation even when you change the global default.

## Pin scopes

Open the user menu with the **👤** button. Each persona can be pinned to:

- **This conversation**: affects only the open conversation.
- **This character**: becomes the persona for new conversations with that character.

The conversation option requires an open conversation. The character option
requires a saved character.

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
