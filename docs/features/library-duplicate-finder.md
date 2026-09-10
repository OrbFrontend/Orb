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

Each result names the evidence that caused it, such as identical content, the
same description and greeting, nearly identical text, or the same avatar with
text that differs.

## Review and resolve

Choose **Compare** to inspect exactly two cards side by side. The comparison
shows descriptions, greetings, tags, World names, avatar presence, conversation
counts, last-used dates, and group-chat collisions.

To remove a redundant copy, select the keeper in the comparison. A card with
conversations cannot be deleted silently: the confirmation relinks its solo and
group-chat references to the keeper first. If both cards are active in the same
group chat, the redundant member slot is removed because the keeper is already
present. Conversation fields that mirror the card, such as the name and
scenario, are refreshed from the keeper.

Deletion is a single-card, confirmed action in this first version. Field-level
merging, conversation consolidation, and batch undo are planned for a later
version.

## Keep intentional variants

Use **Keep both** for cards that are deliberately different versions. The
finder remembers the selected pair (or all pairs in a strong group) and hides
it from future scans. A dismissal automatically expires if either card's
meaningful identity content changes. Retagging or generating a public profile
does not make it reappear. Use **Undo dismissal** immediately after hiding a
match to restore it.

Scan results stay only in the open Manager panel. Close it and run a new scan
when you want a fresh review.
