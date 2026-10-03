# State Fragments

A state fragment remembers story facts across replies, such as trust, inventory,
or unfinished plot threads. The facts stay until you or the Agent change them.
Each conversation branch keeps its own state.

## Starter fragments

Enable these in **Interactive Fragments**. Both start disabled.

- **Notes** holds notes you write yourself. When enabled, its contents are sent
  to **both Director and Writer on every turn**, so what you write can guide the
  scene and the reply. Only you change these notes, in **Inspector → State**.
- **Inventory** tracks items characters hold. The Agent updates the list after
  each reply and sends it to both Director and Writer on later turns.

## Settings

Create or edit an [interactive fragment](director.md#interactive-fragments) and
set its field type to **State**.

**Mode** chooses what it remembers:

- **One value**: one piece of text, such as `Trust: wary`. Each update replaces it.
- **Multiple entries**: a list, such as items or open plot threads. Updates add
  entries and retire ones that no longer apply.

**Update** chooses when it changes:

- **After the reply** (default): records what happened in the saved reply. The
  change is available for the next reply.
- **Before the Writer**: records the Director's plan for this reply, which the
  Writer may not follow. A changed single value sent to the Writer appears as
  `old -> new`.
- **Manual only**: only you change it, in **Inspector → State**.

**Inject** chooses who receives the saved facts: **Off**, **Director**, **Writer**,
or **Both**. This is separate from **Update**: manual notes can still guide
replies, and automatically updated facts can be kept without sending them.

**Description** tells the Agent what to record. **Injection label** is the heading
shown to the Director or Writer. **Required** is available only for a one-value
fragment updated before the Writer.

## The State tab

Open **Inspector → State** to see saved facts and who last changed them. You can:

- Set, edit, or clear a single value.
- Add, edit, or retire list entries.
- Open **History** to see changes on this branch.

Your edits belong to the latest message. Wait until reply generation finishes
before editing.

Disabled fragments keep their saved facts but show them read-only. Deleted
fragments also leave their saved facts here; **Delete saved state** removes those
facts from every branch of the conversation.

## When updates run

Automatic updates need the **Agent** enabled. **Before the Writer** also needs
the **Direction** tool. **After the reply** adds an Agent call; **Individual
fragment processing** updates each fragment separately.

**Manual only** stops automatic updates but still sends saved facts according to
**Inject**. Disabling a fragment stops both updating and sending its facts.
A cooldown pauses updates while the saved facts are still sent.

Skipped or failed updates leave saved facts unchanged. Empty text does not clear
them. The Agent can replace a single value, but only you can clear it. For lists,
the Agent corrects an entry by retiring it and adding a replacement.

## Limits

| Limit | Value |
|---|---|
| Characters per value or entry | 800 |
| Active entries per fragment | 12 |

A list marked **Full** needs an entry retired before another can be added.
Duplicate entries and invalid changes are refused; the Inspector shows why.

Keep facts short: everything you send to the Director or Writer adds to the
prompt on every turn.

## Changing the mode

Changing **Mode** keeps saved facts:

- **One value → Multiple entries**: the value becomes the first entry.
- **Multiple entries → One value**: the entries stay until the next value update
  replaces them. Edit them first if you want to choose the combined wording.

## Branches, regeneration, and group chats

Switching branches shows that branch's facts. Regenerating or editing a reply
starts from the state before that reply.

Your corrections carry into regeneration. The discarded reply's Agent updates
do not. A correction to an entry added by that discarded reply cannot carry over;
the Inspector reports it.

A stopped reply saved as partial text keeps its before-Writer changes but skips
after-reply updates. If no reply is saved, state does not change.

In group chats, state updates once per exchange. **Checkpoint** copies the state
and its history to the new conversation. **Compress History** keeps saved facts
when it replaces older messages with a summary.

The Inspector's **State (this reply)** block shows that reply's changes, your
corrections, and any refused or dropped changes.
