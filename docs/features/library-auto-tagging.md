# Library Auto-tagging

Use auto-tagging to organize your character cards with tags chosen from a list
you provide.

Open the character browser and select **🛠 Manager**.

## Add tags

Enter tags under **Tag vocabulary**, then select **Save vocabulary**.

- You can add up to 64 tags.
- Each tag can be up to 40 characters long.
- Orb can assign only tags from this list.

Use specific, distinct tags. The character browser combines selected tags with
**AND**, so selecting several tags shows only cards that have all of them.

## Tag cards

Select **Tag N characters**, review the warning, and confirm. Orb sends each
card to the Agent model and applies the tags it chooses. The existing tags on
each card are replaced.

Progress is saved after each card. You can cancel the run and continue later;
the next run processes only cards that still need tagging.

A card needs tagging when it is:

- new;
- edited after its last tagging; or
- affected by a newly added vocabulary tag.

Reordering the vocabulary does not require another model run.

### Removing tags

Removing a vocabulary tag removes it from every card that Orb has auto-tagged.
This does not use the model, and Orb asks for confirmation before making the
change.

Adding the tag back later does not restore its old assignments. It makes the
cards eligible for tagging again.

### Tagger thinking

Enable **Tagger thinking** for more deliberate results. It takes longer and
uses more model resources.

## Before you start

Auto-tagging replaces the card's existing tags, including imported or manually
entered tags. This cannot be undone in Orb.

Exports contain the new tags. Export a card before tagging if you want to keep
its original tags.

Editing a card after tagging marks it for tagging again, so manually entered tags
may be replaced by the next run.

## Model and failed cards

Auto-tagging uses the Agent model. If you have configured a separate Agent
endpoint, Orb uses it; otherwise it uses the Writer endpoint.

Cards that fail remain unchanged and are tried again the next time you run
auto-tagging. Orb stops after five consecutive failures so a broken endpoint does
not keep sending requests for the entire library.
