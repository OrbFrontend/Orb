# Library Auto-tagging

Orb can read every character in your library and tag each one from a vocabulary
you write, so the character browser's tag chips filter on terms that mean
something to you rather than on whatever the cards were uploaded with.

Open the character browser and select **🛠 Manager**.

## The vocabulary

Type your tags into the chip box and select **Save vocabulary**. Up to 64 tags,
40 characters each. The model is shown this list and may use nothing else.

Keep it small and keep the tags distinct. The browser's chip filter is an AND,
so tags that apply to most of the library filter to nearly everything and tell
you nothing.

## Tagging

Select **Tag N characters**. Orb sends each card to the Agent model in turn and
writes back the tags it chose. Progress shows a card at a time, and each card is
saved as it finishes, so cancelling or closing the modal keeps the work already
done. Press again to pick up the rest.

The button says **Everything is up to date** when there is nothing to do.
Pressing it again then costs nothing, because a card is only re-tagged when:

- it is new,
- it has been edited since it was tagged, or
- you added a tag to the vocabulary.

Deleting or reordering vocabulary tags costs no model calls at all. A deleted tag
is stripped from the cards that carry it.

**Enable tagger thinking** asks the model to reason before answering. It is
slower and more expensive per card, and worth it only for a vocabulary with
distinctions a quick read would miss.

## What it overwrites, and how to get it back

A card has one set of tags. Tagging **replaces** the tags a card was imported
with — they are not merged and not kept in a second list.

The originals are not lost. Orb stashes them the first time a run touches a card,
which means:

- **Exports still carry the creator's tags.** A card you export or share carries
  what its author wrote, never your private vocabulary.
- **Restore imported tags on N** appears in the Manager panel once a run has
  taken any. It puts every card back and makes them pending again, so it is a
  toggle rather than a one-way door.

Two things it does not cover. Tags you type by hand in the character editor are
not stashed — editing a card also marks it for re-tagging, so the next run will
replace them. And cards tagged before this stash existed have nothing to restore.

## Requirements

The run uses the Agent model. On a separate agent endpoint it uses that one;
otherwise it uses the writer's. A dead endpoint stops the run after five failures
in a row rather than working through the whole library.

Cards that fail are left untouched and stay pending, so pressing **Run** again
retries only those.
