# Macros

A **macro** is a small placeholder you type into text — `{{char}}`, `{{random::red::blue}}` — that Orb swaps out for a real value at the right moment. You write the placeholder once; Orb fills it in with the character's name, a die roll, or a random pick.

They work almost anywhere text ends up in the prompt: chat messages, greetings, personas, scenarios, lorebook entries, fragment text, direction notes.

## The macros

| Macro | What it becomes | Example |
|---|---|---|
| `{{user}}` | Your name (or the active persona's name) | `Hi {{user}}!` → `Hi Alice!` |
| `{{char}}` | The character's name | `{{char}} smiles.` → `Seraphina smiles.` |
| `{{roll::NdM}}` | The total of rolling N dice with M sides each | `{{roll::2d6}}` → `9` |
| `{{random::a::b::c}}` | One of the options, picked at random | `{{random::red::green::blue}}` → `green` |
| `{{pick::a::b::c}}` | Same as `{{random}}` — just a different name | `{{pick::heads::tails}}` → `tails` |
| `{{time}}` | The current local time, 24-hour `HH:MM` | `It's {{time}}.` → `It's 14:32.` |
| `{{date}}` | The current local date, `YYYY-MM-DD` | `Today is {{date}}.` → `Today is 2026-07-16.` |

A few grammar notes for `{{random}}` (and its alias `{{pick}}`):

- Separate options with `::`. Two options or twenty — any number works.
- Options can be whole sentences, and can even span multiple lines.
- Options can't *contain* `::` or `}}` — those characters end the option or the macro.
- Capitalization doesn't matter: `{{RANDOM::a::b}}` works too.

## When does a macro "roll"?

`{{user}}` and `{{char}}` are simple: wherever they appear, they always become the current names. If you switch personas mid-conversation, older text follows along.

The random macros are more interesting. The question is *when* the dice are thrown, and Orb picks the moment that makes sense for each place:

| Where you wrote it | When it rolls |
|---|---|
| A message you send | Once, when you hit send. The result is saved and never changes. |
| A character's greeting | Re-rolls every time you open the conversation — until you send your first message, which freezes it. Great for variety in openers. |
| Persona / scenario / example messages | Rolls once per conversation and then stays put. The same conversation always sees the same pick; a *new* conversation rolls its own. |
| A mood fragment's prompt text | Once per conversation. A randomized style is chosen when the mood first activates, then stays consistent. |
| A value the [Director](director.md) writes | Fresh roll **every turn**. See [Randomizing with {{random}}](director.md#randomizing-with-random). |

!!! tip "The mental model"
    Things that describe *who someone is* (persona, scenario, fragment text) roll once and stay stable, so the story doesn't contradict itself. Things that describe *what happens right now* (your message, a Director decision) roll fresh at that moment.

    `{{roll}}` and `{{random}}` follow the same table. A spaghetti monster persona with `{{roll::3d8}} limbs` grows its limbs once per conversation and keeps them; a `{{roll::1d20}}` in your message is thrown the moment you hit send.

`{{time}}` and `{{date}}` are the exception: they always resolve to *now*, so seeding can't pin them. In a message they freeze at send time, like the other macros. In persona/scenario text they become the current time/date on **every** turn — which also means the prompt prefix changes over time and defeats KV caching, so prefer them in messages and greetings.

Checkpoints keep their parent's rolls: if you branch a conversation, the copy inherits the same random picks instead of re-rolling, so both branches share the same past.

## Keeping a macro as plain text

Sometimes you want to *show* a macro without triggering it — for example, telling the Director "output `{{random::yes::no}}`" in a fragment description, or explaining macros to someone in chat.

Wrap it in single backticks:

```
Use `{{random::heads::tails}}` to flip a coin.
```

Anything inside backticks is left exactly as written — no substitution, no rolling, anywhere in Orb. The backticks stay in the text, so it also reads as quoted code to the model.

## Handy examples

A greeting that opens differently each time you re-enter (until you reply):

```
{{random::The tavern door creaks open.::Rain hammers the windows.::The fire has burned low.}} {{char}} looks up at {{user}}.
```

A dice check in your own message:

```
I try to pick the lock. ({{roll::1d20}})
```

A scenario detail that's different in every playthrough, but consistent within one:

```
The heist takes place in {{random::a bank vault::a moving train::a masquerade ball}}.
```
