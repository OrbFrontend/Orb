# Library Duplicate Finder

The Library Duplicate Finder helps you find cards that are copies of each
other, even when one was lightly edited or imported with a different avatar.
It never deletes anything without a confirmation.

## Scan the library

Open **Browse → Manager → Duplicate finder** and choose **Scan**. The scan is
local and deterministic: it does not call a model or send card content
anywhere. Results belong to the open Manager panel, so close it and scan again
for a fresh review.

## Review a match

The finder shows two kinds of result:

- **Strong match** means there is enough shared content to group the cards.
- **Possible match** means the cards look related, but you should inspect them
  yourself.

Each result shows the cards' avatars, conversation counts, and age. Cards are
labelled **A**, **B**, **C**, and so on, so you can tell identical names apart.
The **Most used** label is only a suggestion; choose the card you actually want
to keep.

Open **Why these matched** to see the evidence. Choose **Compare** to view two
cards side by side. Differences in the description, personality, scenario,
greetings, tags, and World are shown inline; identical fields stay collapsed.

## Keep one card

Choose **Keep this one** beside the card you want. Orb shows a confirmation that
names the cards being removed and the conversations that would move. Confirming
keeps your chosen card and deletes the others in that match.

If a card is used by conversations, Orb moves those references first. In a group
chat where both copies are present, it removes the redundant slot. A failed
check leaves the library unchanged.

## Keep intentional variants

Choose **Not duplicates** when a match is deliberate. That match stays hidden
until one card's meaningful content changes. Changing tags or a generated public
profile does not bring it back. Use **Undo** right away if you hid the wrong
match.
