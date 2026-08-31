# Direction Notes

A direction note is an interactive fragment that saves a story fact for later
turns. Notes belong to the active conversation branch and remain until you edit
or delete them.

## Recording and injection

Open **Settings → Agents → Direction Notes**. The two controls are independent:

- **Recording** asks the Agent whether the turn produced a durable fact and saves
  notes for enabled direction-note fragments.
- **Injection** chooses who receives saved notes: **Off**, **Director**, **Writer**,
  or **Director and writer**.

You can inject existing notes without recording new ones. Turning recording off or
disabling a fragment does not delete notes already saved.

Each direction-note fragment has a **When recorded** setting:

- **End of turn**: records after the reply is complete.
- **Before writer**: records after the Director and before the Writer. This option
  requires an active Director scene-direction step.

## Add notes yourself

When recording or injection is enabled, the Notes panel lists notes on the active
branch. The note button on an assistant reply lets you add a label and note text
to that turn. Your notes are marked **You**. You can edit or delete notes from the
panel.

## Branches

Regenerating or editing a reply creates a new branch. Notes on the old path do not
appear on the new path. Returning to the old branch shows its notes again.

In a group chat, recording runs once for the exchange rather than once per
speaker.

## Enable it

1. Open **Settings → Agents → Direction Notes**.
2. Turn on **Recording**, choose an **Injection** target, or both.
3. Enable at least one direction-note fragment.

The feature requires the global **Agent** toggle.
