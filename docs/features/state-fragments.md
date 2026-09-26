# State Fragments

A state fragment keeps something true across turns: where the scene stands, how
far a character trusts the user, or the story threads still open. Its state
belongs to the active conversation branch and returns on every later reply until
the Agent or you change it.

State fragments are interactive fragments with the field type **State**.

## Settings

Each state fragment has three settings in the fragment editor.

**Mode**

- **One value**: a single text, such as `Trust: wary, since the lie about the key`.
  Each update replaces it.
- **Multiple entries**: a list of up to 12 entries, such as open plot threads.
  Updates add new entries and retire entries that no longer hold.

**Update**

- **After the reply** (the default): the Agent updates the fragment after the
  reply is saved, from what the reply actually showed. The change reaches the
  next reply.
- **Before the Writer**: the Agent updates the fragment before the reply is
  written. It records the Director's intent for this reply, which the Writer may
  not carry out. The Writer sees the change in the same reply, and a changed
  value appears as `old -> new`.
- **Manual only**: only you change the fragment, in the Inspector's State tab.

**Inject**: **Off**, **Director**, **Writer**, or **Director and Writer** choose
who reads the saved state. Injection and updating are independent: a fragment
can be updated without being injected, and a manual fragment can still be
injected. The Agent always sees the current state of the fragments it updates.

The **Injection label** is the fragment's heading in the state block, and the
**Description** tells the Agent what to record. **Required** appears only for a
one-value fragment updated before the Writer, because that value is part of the
Director's scene direction.

## When updates run

Before-Writer updates need the Agent and the **Direction** tool. A one-value
fragment is filled in the Director's scene direction call itself; a
multiple-entry fragment gets its own call right after it. While the Agent or
Direction is off, a before-Writer fragment keeps its state.

After-reply updates run as one extra Agent call after the reply, which reuses the
cached prompt. The **Individual fragment processing** option in the Direction
card also applies: it updates each fragment in its own call.

Switching a fragment to **Manual only** stops its automatic updates. Its saved
state is still injected, and you can still edit it in the Inspector's State tab.
Disabling a single fragment stops both its updates and its injection, but keeps
its saved state; the State tab shows that state read-only.

A fragment on cooldown keeps and still injects its state; it is only not updated.

## What an update can do

Anything the Agent leaves out stays as it is. An omitted fragment, a skipped
call, an invalid tool call, a cooldown, or a model error never changes saved
state, and empty text never clears anything.

- For a one-value fragment, the Agent can only write a new value. It cannot
  clear the value; to say something no longer applies, it writes a value such as
  "none".
- For a multiple-entry fragment, the Agent can add entries and retire listed
  ones. To correct an entry, it retires it and adds the corrected text.

Orb refuses an operation that names an unknown fragment or entry, repeats an
active entry exactly, or exceeds a limit, and reports it in the Inspector. The
limits are fixed:

| Limit | Value |
|---|---|
| Characters per value or entry | 800 |
| Active entries per fragment | 12 |

When a list holds 12 entries, a further add is refused and the fragment shows
**Full** in the State tab until an entry is retired. The Agent sees each list's
count and is told when a list is full.

### Prompt size

Every injected fragment adds to the Director's or Writer's prompt on every turn.
At the limits, a one-value fragment adds up to about 800 characters (about
1,600 to the Writer on a turn that shows it as `old -> new`) and a
multiple-entry fragment up to about 9,700 (12 entries of 800 characters plus
their bullets). Keep descriptions specific so entries stay short, and inject a
fragment only where it is read.

## The State tab

The Inspector has a **State** tab beside **Main**. The tabs appear whenever an
enabled state fragment exists or the conversation holds saved state.

The tab lists each fragment with its current value or entries, the turn that
set each one, and who set it: **Agent**, **You**, or **Carried** (state that
Compress History kept on its summary). From the tab you can:

- set, edit, or clear a one-value fragment;
- add, edit, or retire entries of a multiple-entry fragment;
- open a fragment's **History**, the changes on this branch, newest first.

Your changes are attached to the latest message on the branch. While a reply is
being generated, the tab refuses edits; make them once it finishes.

Disabled fragments are shown read-only. A fragment you deleted still shows its
saved state read-only under its saved label, with **Delete saved state** to
remove it from the conversation on every branch.

## Changing the mode

Changing a fragment's mode never rewrites or discards saved state; it changes
which operations the Agent and the State tab offer, from the next turn.

- One value to multiple entries: the value becomes the first entry.
- Multiple entries to one value: the entries stay. The fragment shows them
  together as its current value, and the State tab notes that the next update
  replaces them with one value. Merge them first by editing and retiring
  entries if you want to choose the wording; otherwise the Agent's next value
  replaces them, and the originals remain in history.

## Branches, regeneration, and group chats

State follows the branch. Regenerating or editing a reply starts from the parent
message's state; switching back to another branch shows that branch's state.

A correction you made on a reply carries over when you regenerate that reply,
and the new reply is written with it. The Agent's own changes on the discarded
reply are not carried; the new turn makes its own. A carried edit or retirement
of an entry that the discarded reply had added has nothing to apply to, so it is
dropped and listed in the Inspector.

A stopped reply saved as partial text keeps the before-Writer changes that
shaped it. After-reply updates do not run for it. A turn that saves no reply
changes no state.

In a group chat, state updates run once per exchange, not once per speaker.

**Checkpoint** copies the branch's state and its history into the new
conversation. **Compress History** keeps the state that the removed messages had
built, on the summary message, so nothing is lost.

## Inspector

The Inspector's **State (this reply)** block lists the changes a reply made and
the changes you made on it, marked **You**; operations Orb refused, with the
reason; and carried corrections that were dropped.

## Starter fragments

A disabled **Inventory** starter tracks the items each character holds:
multiple entries, updated after the reply, injected into both. Enable it in
**Interactive Fragments**, or use it as an example.

A **Notes** fragment (multiple entries, manual only) holds free-form notes you
write yourself.
