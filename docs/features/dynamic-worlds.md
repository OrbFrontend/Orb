# Dynamic Worlds

Let the Agent keep a [World](lorebooks.md) up to date with what happens in play. It reads the turn that just finished, and when something durable was established it **proposes** an entry. Nothing is written until you accept it.

This is the write side of lorebooks. [Agentic Lorebook](agentic-lorebook.md) is the read side — it picks which existing entries to inject. The two are independent; either is useful without the other.

## Turning it on

Open a lorebook and flip **Dynamic World**, the toggle at the top of the entry list. That's the whole setup, per World.

A World is a target for proposals when three things are true:

- the global **Agent** toggle is on,
- the World is **enabled**,
- **Dynamic World** is on for it.

Being enabled matters as much as opting in: an enabled World fed this turn's prompt, so the exchange is evidence about it. A disabled one contributed nothing to the scene and learns nothing from it.

Dynamic Worlds wear a halo ring in the sidebar, the same one a character with an expression pack gets.

!!! note "It is not tied to the character card"
    Every enabled World that opted in is a target, not just the one linked to the character you're chatting with. Several are routinely in play at once; each gets its own proposal.

## What gets proposed

After the reply is finished — after the editor, after any workflow that rewrites the draft — the Agent is asked one question: did this exchange establish anything worth filing?

It can answer three ways per entry:

| Verb | Means |
|---|---|
| **Add** | Something no entry covers yet |
| **Revise** | An existing entry is now wrong; here's what it should say |
| **Retract** | An existing entry no longer holds, and nothing replaces it |

Each proposed entry says how it should activate — **Always in context** (constant) or **Keyword-activated** — and carries a one-line rationale for why it belongs in the World instead of just in the chat history. A revise that doesn't say inherits how the entry it revises already activated, so correcting what an entry says never quietly changes when it shows.

The instructions it works under are deliberately narrow: an entry is something that stays true once the moment has passed, stated plainly in a sentence or two. Plans, guesses, and things merely considered don't count. Neither does rewording an entry that's already right. **Most turns propose nothing, and that's the expected answer** — a failed or empty proposal costs you nothing and never touches the reply.

## Reviewing

A proposal shows up as a card under the reply that produced it, and in the lorebook drawer's **Pending** tab (the tab badge and the sidebar count tell you how many are waiting).

Each card shows the before/after text of every operation and offers:

- **Apply** — write it into the World.
- **Edit** — reword entries, change activation or keywords, or untick ones you don't want. The batch commits together.
- **Reject** — throw it away.

Decided proposals leave the reply and move to the drawer's **History** tab.

## Your entries and the Agent's

The Agent writes to its own layer. Entries it created are tagged **Dynamic** in the entry list and carry a banner in the editor.

**It can never modify or delete an entry you wrote.** A revise of one of your entries doesn't edit it — it writes a dynamic entry that hides yours and stands in its place. A retract hides yours and injects nothing. Your originals sit underneath, untouched.

That's what makes the escape hatch cheap: **Reset** (next to the Dynamic World toggle) retires every Agent-managed entry at once, leaving the authored World exactly as you wrote it. The reset itself is undoable from History.

You can still edit a dynamic entry by hand if you'd rather fix than reset — it's an ordinary entry with an owner tag.

## When the World moves on

A proposal is judged against the World as it stood when the turn ran. Edit that World in the meantime — or edit the message the proposal came from — and the proposal goes **stale**: it drops its Apply button and offers **Re-evaluate** instead.

Re-evaluate re-runs the same question against the World as it now stands, replacing the old proposal with a fresh one (or with nothing, if the answer changed to "nothing to file").

There is no force-apply and no automatic merge. Two changes can touch different entries and still contradict each other in meaning, so the fix is always to re-judge, never to overwrite.

## History and undo

The **History** tab lists everything decided — applied, rejected, undone, and proposals replaced by a re-evaluation — each row leading with the state it ended in, plus the character and date it came from.

An applied change keeps an **Undo** button while every entry it wrote still matches what it wrote. Edit one of those entries by hand and the undo is refused rather than clobbering your edit.

Deleting an entry by hand from a Dynamic World is recorded in History too, so the log shows what happened to the World rather than only what the Agent did to it. A delete is the one thing with no Undo button — the row is gone.

!!! note "History is not per-branch"
    Conversations branch; a World doesn't. Once you accept a change it's canon for every character sharing that World, even if the branch it came from is later regenerated or abandoned. Pending proposals, meanwhile, are invisible to everything — the prompt, the projection, other chats — until you accept them.

## Where the entries end up

Accepted entries are ordinary lorebook entries and follow the [usual rules](lorebooks.md), with one difference in presentation: they render after your own, under a **Dynamic World State** heading. Constant ones ride the system prompt; keyword ones appear in the per-turn block when their keywords hit.

A character sharing the World sees them on the next reply generated after acceptance.

## Export

**Export JSON** writes your authored entries only, and so does the lorebook embedded in an exported character card. Share a card and you share the lorebook you wrote, not one chat's accumulated state. Exporting the effective state — yours plus the accepted overlay — is an explicit opt-in on the API (`?view=effective`).

Orb [preset backups](backups-presets.md) carry everything: dynamic entries, pending proposals, and the full history.

!!! tip "Cost"
    The proposal is one extra tool call at the end of the turn, reusing the prompt the Director, Writer, and Editor already built — so it re-bills almost nothing. See [KV Cache Reuse](../architecture/kv-cache.md).
