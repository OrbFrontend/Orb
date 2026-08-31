# Dynamic Worlds

Dynamic Worlds let the Agent suggest lorebook changes based on what happens in a
conversation. Nothing changes until you review and accept a proposal.

[Agentic Lorebook](agentic-lorebook.md) chooses existing entries. Dynamic Worlds
propose new entries, revisions, and retractions. The two features work separately.

## Enable a World

Open the World and turn on **Dynamic World**. Proposals are generated only when:

- The global **Agent** toggle is on
- The World is enabled
- **Dynamic World** is enabled for that World

Every enabled Dynamic World can receive a proposal. The feature is not limited to
the World linked to the active character.

## Proposal types

| Type | Meaning |
|---|---|
| **Add** | A durable fact is not covered by an existing entry. |
| **Revise** | An existing fact changed and needs new text. |
| **Retract** | An existing fact is no longer true. |

Proposals include activation choices such as **Always in context** or
**Keyword-activated**, plus a reason for recording the fact. The Agent is asked to
record durable facts, not plans, guesses, or details that are already correct.
Most turns produce no proposal.

## Review proposals

Proposals appear below the reply that produced them and in the World's **Pending**
tab. Use:

- **Apply** to write the proposal to the World
- **Edit** to change its text, activation, or keywords before applying
- **Reject** to discard it

Decided proposals move to **History**.

## Agent entries and your entries

Entries created by the Agent are marked **Dynamic**. The Agent never edits or
deletes an entry you wrote. A revision or retraction of your entry creates a
dynamic overlay; your original remains unchanged.

Use **Reset** beside the Dynamic World toggle to retire all Agent-managed entries.
The reset can be undone from History. You can also edit a dynamic entry directly.

## Stale proposals and history

A proposal becomes **stale** if the World or its source message changes before you
apply it. Select **Re-evaluate** to create a new proposal against the current data.
Orb does not force-apply a stale proposal.

History records applied, rejected, undone, and re-evaluated proposals. An applied
change can be undone while its entries still match the applied version. A manual
edit prevents that undo from replacing your change.

Worlds are shared and are not branched with conversations. An accepted change is
available to every character and conversation that uses the World. Pending
proposals remain private until accepted.

## Export

Normal JSON export and lorebooks embedded in exported character cards contain your
authored entries. Preset backups include dynamic entries, pending proposals, and
history. The effective World state is available only through the API's explicit
`?view=effective` option.
