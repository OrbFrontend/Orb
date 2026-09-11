# Library duplicate finder

The Character Library Manager includes an explicit duplicate scan. It helps
surface cards that were imported from different sources, re-encoded, tagged
differently, or lightly edited over time.

Open **Browse → Manager → Duplicate finder**, then choose **Scan**. The scan is
local and deterministic: it does not call a model or send card content anywhere.
It recomputes text signals for the current library and caches only decoded
avatar fingerprints, so subsequent unchanged scans are quick.

## What counts as a match

The finder compares normalized card identity fields, narrative-text overlap,
and decoded avatar pixels. Tags, generated public profiles, Worlds, and other
organizational metadata do not stop otherwise identical cards from matching.
A matching name by itself is never enough.

Results are separated into:

- **Strong matches**, grouped when there is decisive shared content or very
  similar text with corroborating name or avatar evidence.
- **Possible matches**, which are independent pairs for review. They are never
  chained into a larger group merely because each resembles a third card.

Both kinds are presented the same way, because a possible pair is simply a
cluster of two. Each result lists its member cards and, under **Why these
matched**, the evidence that caused it: identical content, the same description
and greeting, nearly identical text, or the same avatar with text that differs.

## Telling identical copies apart

Duplicates are usually the one case where names identify nothing — three cards
all called *Reimu* make every "keep Reimu" button read the same. So every member
of a cluster carries:

- a letter (**A**, **B**, **C**, …) that stays with that card through the
  comparison view and onto the button that keeps it,
- its avatar,
- how much it is used and how old it is, such as *3 conversations · last used
  20d ago · added 4 Apr*.

Members are ordered with the likeliest keeper first: most conversations, then
oldest. The one with strictly the most conversations is marked **Most used**.
That ordering is a suggestion, not a decision — the letters are assigned from
it, so they mean the same thing in the list and in the comparison.

## Review and resolve

Choose **Keep this one** on any member to keep that card and delete the rest of
the cluster. This is a single confirmed action whether the cluster holds two
cards or five; the confirmation names each card being deleted by the same facts
the list shows, and says how many conversations will move.

Choose **Compare** under **Why these matched** to inspect exactly two cards. Every field — descriptions, greetings, tags, World names — is shown once
as a single inline diff: shared text is plain, struck text belongs only to the
first card, and highlighted text only to the second. Fields that differ are
expanded; identical and empty ones collapse to a labelled row you can open. The
comparison also shows avatar presence, conversation counts, last-used dates and
group-chat collisions, and ends in the same **Keep this one** choice for either
side.

A card with conversations is never deleted silently: the confirmation relinks
its solo and group-chat references to the keeper first. If both cards are active
in the same group chat, the redundant member slot is removed because the keeper
is already present. Conversation fields that mirror the card, such as the name
and scenario, are refreshed from the keeper. A cluster removal is checked in
full before anything is deleted, so a refusal leaves the library untouched.

Field-level merging, conversation consolidation, and batch undo are planned for
a later version.

## Keep intentional variants

Use **Not duplicates** for cards that are deliberately different versions. The
button in the match header hides every pair in that match. A match of more than
two cards also puts one on each evidence row, which hides just that pair; at
exactly two cards the pair *is* the match, so only the header button is shown.
A dismissal automatically expires if either card's meaningful identity content
changes. Retagging or generating a public profile
does not make it reappear. Use **Undo** immediately after hiding a match to
restore it.

Scan results stay only in the open Manager panel. Close it and run a new scan
when you want a fresh review.
