# Card-Embedded Fragments

You can ship mood and interactive fragments **inside a character card** so the character arrives with its own curated direction toolkit. Ships to friends, uploads to a hub, re-imports — and the fragments come with it.

## Why embed fragments in a card?

Orb's [Scene Direction](director.md) fragments (moods and interactive blanks the Director fills) normally live in your global library. They're *your* tools, shared across all conversations. A character card that ships its own fragments means the creator can pre-tune how the Director steers that specific character:

- A horror character ships a `dread` mood with atmospheric prompt text, plus a `sanity` progressive that creeps down as the story unwinds.
- A courtly-drama character ships a `scandal` interactive listing active rumours, a `reputation` progressive, and a `court_gossip` feedback note.
- A quest-giver NPC ships a `quest_phase` string the Director updates as milestones pass.

Card fragments are **author-curated direction presets** — the character arrives with a Director that knows what kind of story to tell with it.

## How merging works

When a conversation is active with a character that has card-embedded fragments, Orb merges them with your global fragments **each turn** before the Director runs:

1. Your global fragments are loaded first — moods and interactive, only the enabled ones.
2. Card fragments are appended — but only if their **ID** doesn't already exist in your global list.
3. Any card fragment whose ID matches a global ID is silently dropped.

The rule: **your globals always win.** A card can suggest a mood or interactive, but it can never replace or override one you've already configured.

This merge is computed fresh every turn. If you change your global fragments mid-conversation — disable one, add one, rename one — the merge updates on the next reply. The character's card-embedded fragments stay constant (they're baked into the PNG), but which ones survive the merge changes with your configuration.

## Authoring card-embedded fragments

Open a character's edit modal and switch to the **Fragments** tab.

![The Fragments tab in the character edit modal](../assets/screenshots/char-fragments-edit-modal.png)

It mirrors the global fragment editor, but edits here only modify the card's copy — they never touch your global library.

If you delete a card fragment and save, the fragment is gone from the card. If you add a card fragment whose ID collides with a global, it'll be hidden at runtime (the global wins); the editor shows both the global and the card version side by side so you can see the conflict.

Card fragments are validated with the same rules as globals: the ID must match `[a-z0-9][a-z0-9_-]{0,63}` (up to 64 chars, lowercase letters/digits/underscores/dashes), labels have length limits, `field_type` must be one of the five recognized types, and `direction_note_timing` must be `pre_writer` or `post_turn`.

## What the character sees (and doesn't)

Card-embedded fragments go straight into the Director's `direct_scene` tool alongside your globals, so the Director fills them exactly the same way. The Writer sees the resulting values in its Scene Direction block. Everyone downstream — Writer, Editor, feedback step — treats them identically to global fragments.

They never appear in your global fragment sidebars (unless you manually add a matching entry). They're per-character luggage that travels with the card file.

## Sharing and importing

Card fragments are stored in the character card's V2 `extensions` dict at the key `orb.fragments`. When you export the card as a PNG, the fragments travel inside it. When you (or someone else) imports the card, the fragments arrive embedded in the `extensions` column.

If you import a card that someone else authored with fragments, those fragments become available the moment you start a conversation with that character — no setup needed. If a fragment's ID collides with one of yours, yours wins (per the merge rule above), so importing can never silently replace your configuration.

## Trust and validation

Card PNGs come from third parties, so card-embedded fragments are parsed inside a **trust boundary** — every field is type-checked and range-limited before it reaches the Director:

| Guard | Limit |
|---|---|
| Mood fragments per card | 50 |
| Interactive fragments per card | 50 |
| Total embedded fragments (either type) | 50 per type |
| `field_type` must be one of | `string`, `array`, `progressive`, `feedback`, `direction_note` |
| `direction_note_timing` must be | `pre_writer` or `post_turn` |
| Enabled fragments only | `enabled: false` entries are skipped |
| Duplicate IDs (within the card) | first wins |

A malicious or broken card simply won't load its extras — Orb never crashes on a bad fragment blob.

## Fragment format reference

When you hand-author a card's JSON (e.g. in a card-creation tool), place fragments under `extensions.orb.fragments` inside the card's `data` object:

```json
{
  "spec": "chara_card_v2",
  "data": {
    "name": "My Character",
    "description": "...",
    "extensions": {
      "orb": {
        "fragments": {
          "mood": [
            {
              "id": "dread",
              "label": "Dread",
              "description": "Use when the atmosphere turns oppressive or frightening",
              "prompt_text": "Write with creeping dread. Let silences feel heavy. Every detail should feel ominous.",
              "negative_prompt": "Relax the tension. Return to a calm, matter-of-fact tone.",
              "enabled": true
            }
          ],
          "interactive": [
            {
              "id": "suspicion_target",
              "label": "Suspicion Target",
              "description": "Which character or faction the protagonist currently suspects. Pick the most narratively tense option.",
              "field_type": "string",
              "injection_label": "Suspicion",
              "required": false,
              "sort_order": 1,
              "direction_note_timing": "post_turn"
            }
          ]
        }
      }
    }
  }
}
```

If you don't need one of the two lists, omit it — an empty or missing `mood` or `interactive` key is fine. If both are missing or `fragments` itself is missing, the card loads normally without any fragments.

!!! tip "Sort order"
    Card-embedded interactive fragments get an automatic sort-order offset of 10,000, so they always appear at the bottom of the Director's `direct_scene` parameters and the Writer's Scene Direction block, after your global fragments. You don't need to think about ordering relative to globals — Orb handles it.

## When card fragments don't run

Card fragments are only active when the character is **in context** for a conversation. They use the same pipeline as global fragments. This means:

- They don't fire for document-mode writing or any mode without an assigned character.
- They don't fire if the character card has been **persona-pinned** — when a persona overrides the card, only the card fragments of the character that is actually in context apply. (The persona's card fragments, if any, would apply instead.)
- They don't fire if the card's `extensions` key is missing, `null`, or has no `orb.fragments`.
