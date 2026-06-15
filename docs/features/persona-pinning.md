# Persona Pinning

Pin a specific user persona to a conversation or to a character so that chat always speaks as the right "you" — no matter which persona is set as the global default.

## Why pin?

Orb has one **default persona** (the one highlighted with the *Default* badge in the user menu). It seeds every new chat and is what generation uses when nothing else applies. That's fine until you juggle several personas: switch the default to write as someone else and every other open chat silently changes who *you* are too.

Pinning solves that. A pin locks a persona to a scope so later default switches leave it alone.

## The two scopes

Open the **user menu** (the 👤 button) to manage pins. Each persona row has two pin buttons:

- **💬 Pin to this conversation** — only this chat uses this persona.
- **💏 Pin to this character** — every *new* chat with this character starts on this persona.

The button is disabled when its scope isn't available (no open conversation, or a chat that isn't tied to a saved character).

## Which persona wins

When a turn is generated, Orb resolves the effective persona top-down:

1. **Conversation pin** — if the open chat is pinned, that persona wins.
2. **Character pin** — otherwise, if the character is pinned, that one is used.
3. **Global default** — otherwise the default persona applies.

The 👤 button reflects the result, and its glyph tells you *how* the shown persona was chosen: 💬 for a conversation pin, 💏 for a character pin, plain 👤 for the global default.

## Pinning is never a trap

Pins are deliberately forgiving:

- **Selecting a persona while a chat is pinned just re-pins the chat** to your new choice — you never get stuck unable to switch.
- **Pinning B while A holds the scope** simply moves the pin to B.
- **On send**, an unpinned chat is automatically pinned to whoever is currently effective (character pin → global default). This freezes who authored the existing turns, so a later default switch can't rewrite the past. An explicit unpin stays in effect until the next send.

## Cleanup

Deleting a persona clears any pins that pointed at it, so chats and characters fall back to the next rule in the precedence chain.
