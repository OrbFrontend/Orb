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

Select **Tag N characters** and confirm — the run replaces tags and cannot be
undone. Orb then sends each card to the Agent model in turn and writes back the
tags it chose. Progress shows a card at a time, and each card is
saved as it finishes, so cancelling or closing the modal keeps the work already
done. Press again to pick up the rest.

The button says **Everything is up to date** when there is nothing to do.
Pressing it again then costs nothing, because a card is only re-tagged when:

- it is new,
- it has been edited since it was tagged, or
- you added a tag to the vocabulary.

Reordering vocabulary tags costs nothing at all.

Deleting one costs no model calls either, but it is a write across the library:
the tag is stripped from every card a run has tagged, and nothing keeps a copy.
Orb confirms first, once anything is tagged. Adding the tag back later does not
bring those assignments back — it makes the whole library pending and tags it
again from scratch, at the same price as the first time.

**Enable tagger thinking** asks the model to reason before answering. It is
slower and more expensive per card, and worth it only for a vocabulary with
distinctions a quick read would miss.

## What it overwrites

A card has one set of tags, and tagging **replaces** it. The tags a card was
imported with are not merged, not kept in a second list, and not recoverable —
the run asks you to confirm before it starts, and that is the only gate.

One tag list is the point rather than a shortcut: the tags you see in the browser
are the card's tags, so a card you export carries what the browser showed you,
and nothing downstream has to choose between two answers.

What follows from that:

- **Exports carry your vocabulary,** not the creator's. Export a card before
  tagging it if you want to keep the tags its author wrote.
- **Hand-typed tags go too.** Editing a card also marks it for re-tagging, so
  tags you type in the character editor are replaced by the next run.

To undo a run, restore a backup, or re-import the cards.

## Requirements

The run uses the Agent model. On a separate agent endpoint it uses that one;
otherwise it uses the writer's. A dead endpoint stops the run after five failures
in a row rather than working through the whole library.

Cards that fail are left untouched and stay pending, so pressing **Run** again
retries only those.
