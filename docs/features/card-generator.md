# Library Card Generator

Library Card Generator turns a character idea into a card you can edit before it
goes into your library.

## Use it

1. Open **Character Library → Manager → Card Generator**.
2. Describe the character. You have 2,000 characters, but a sentence or two is
   enough to start.
3. Choose how much of your library Orb should use, then press **Generate**.
4. Read the draft in the character editor. Change anything you like, add an
   avatar if you want, and press **Save**.

Nothing is saved when you press **Generate**. **Cancel**, closing the library,
or leaving Manager stops the request. If the Agent endpoint has a problem, the
panel shows the error so you can try again.

The draft contains a name, description, personality, scenario, opening message,
example dialogue, and creator notes. It does not add an avatar, tags, or prompt
overrides. Once saved, it behaves like any other new card; Auto-tagging can tag
it later.

## Tailored to me

Your idea always comes first. Tailoring is optional:

- **Off** uses only your idea.
- **Library summary** adds a small summary of your library: its size, tag
  vocabulary, persona names, and the characters you play most. It does not read
  character descriptions or chat messages.
- **Deep: reads your chats** lets the Agent look for patterns in your cards and
  conversations before writing. It takes longer and always enables generator
  thinking.

Deep mode sends excerpts of your library to the Agent endpoint. Choose a
provider you trust with that data. The research is read-only; it cannot change
your library.

<details markdown="1">
<summary>What Deep mode can read</summary>

Deep mode can query only these read-only views and columns:

| View | Columns |
|---|---|
| `conversations` | `id`, `title`, `character_card_id`, `character_name`, `persona_id`, `kind`, `created_at`, `updated_at`, `last_accessed_at`, `active_leaf_id` |
| `messages` | `id`, `conversation_id`, `role`, `content`, `parent_id`, `turn_index`, `created_at` |
| `characters` | `id`, `name`, `description`, `personality`, `scenario`, `first_mes`, `mes_example`, `creator_notes`, `tags`, `alternate_greetings`, `creator`, `source_format`, `created_at`, `updated_at` |
| `user_personas` | `id`, `name`, `description`, `created_at`, `updated_at` |

</details>

## Thinking

**Enable generator thinking** is off by default. Turn it on when you want the
Agent to spend more time on the draft; generation may take longer. Deep mode
turns it on for you and restores your previous choice when you switch away.

Review the result like any other first draft. The editor is the point where you
decide what becomes part of your library.
