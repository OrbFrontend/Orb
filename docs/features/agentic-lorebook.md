# Agentic Lorebook

Let the [Director](director.md) decide which lorebook entries belong in the scene each turn, instead of relying purely on keyword matches.

## Lorebooks in brief

A **lorebook** (grouped under a *World*) is a set of entries — named chunks of background lore, facts, or rules — that get injected into the Writer's context when they're relevant. Each entry can be triggered two ways:

- **Trigger keywords** — the entry activates when one of its keywords appears in the recent messages (the keyword scan looks back 6 messages).
- **Constant** — the entry is *always* injected, regardless of keywords. Toggle this on an entry to make it permanent context.

Keyword triggering is simple and cheap, but blunt: it only fires on a literal substring match. If the conversation circles a topic without ever naming it, the relevant lore stays silent.

## What the agentic mode adds

With **Agentic Lorebook** enabled, the Director takes over activation. On each turn it's handed a compact **catalog** of the available entries (names plus their first few keywords, grouped by World) and picks the ones relevant to the scene. Because the Director actually *reads the room*, it can pull in lore that keyword matching would miss.

The selection runs as its own short `select_lorebook` call during the Director stage, independent of the scene-direction tool — so the cost is one extra lightweight tool call per turn.

## What still happens automatically

The agentic selection is layered *on top of* the deterministic rules, not a replacement:

- **Constant entries** are always injected and are never shown to the Director to manage — they're excluded from the catalog entirely.
- **The keyword scan still runs** (over the current turn) in parallel, so a keyword the Director overlooks still activates its entry. The Director can only *add* to the selection, never suppress a hard keyword hit.

The final set the Writer sees is: constant entries ∪ the Director's picks ∪ keyword matches.

## Enabling it

Open **Settings → Agents** and turn on the **Agentic Lorebook** card (it sits just under the Direction card).

It only needs the global **Agent** on — it works whether or not the Director's scene-direction tool (`direct_scene`) is enabled. It falls back to the plain keyword scan when there are no non-constant entries to choose from — there's nothing to manage, so no catalog is offered.
