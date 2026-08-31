# Card-Embedded Fragments

Card-embedded fragments let a character card include its own mood and interactive
fragments. The fragments travel with the card when you export or share it.

They are the same fragments used by [Scene Direction](director.md). Global
fragments live in your library; card fragments are specific to the character.

## Which fragments are used

For a conversation, Orb combines:

1. Enabled global fragments
2. Enabled fragments from the character card

Global fragments take priority when IDs conflict. In a group chat, the first card
in the cast keeps an ID when two cards conflict. Card fragments are scene-wide, so
a mood from one character can affect the next speaker too.

Changing your global fragments affects the next turn. Card fragments remain in the
card and are not copied into your global library.

## Add fragments to a card

1. Open the character editor.
2. Select **Fragments**.
3. Add a mood or interactive fragment, or edit an existing one.
4. Enable or disable fragments as needed.
5. Save the character.

The editor uses the same fields and validation as the global fragment editor. An
ID must match `[a-z0-9][a-z0-9_-]{0,63}`. A card cannot use an ID already used by
one of your global fragments.

## Import and export

Card fragments are stored in V2 card data under `extensions.orb.fragments`. They
are included when the card is exported as a PNG and loaded again when the card is
imported. The character's owner can edit them; they do not become global
fragments.

## Safety limits

Orb treats card data as untrusted. Invalid fragments are skipped instead of
stopping the import.

| Check | Result |
|---|---|
| More than 50 mood or 50 interactive fragments | Extra fragments are ignored |
| Missing or invalid ID, or empty label | Fragment is dropped |
| `enabled: false` | Fragment is skipped |
| Duplicate IDs in one card | The first fragment is kept |
| Unknown `field_type` | Uses `string` |
| Unknown `direction_note_timing` | Uses `post_turn` |

Valid interactive field types are `string`, `array`, `progressive`, `feedback`,
and `direction_note`. Valid direction-note timings are `pre_writer` and
`post_turn`.

## Card format

Card creators can add fragments under the card's `data.extensions` object:

```json
{
  "spec": "chara_card_v2",
  "data": {
    "name": "My Character",
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
              "direction_note_timing": "post_turn",
              "enabled": true
            }
          ]
        }
      }
    }
  }
}
```

`mood` and `interactive` can be omitted or empty. Card fragments appear after
global fragments in the Director's fields and in the Writer's Scene Direction
block.
