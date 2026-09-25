# Library Auto-tagging

Use auto-tagging to organize your character cards with tags chosen from a list
you provide.

Open the character browser and select **Manager**.

## Add tags

Enter tags under **Tag vocabulary**, then select **Save vocabulary**.

- You can add up to 64 tags.
- Each tag can be up to 40 characters long.
- Orb can assign only tags from this list.

Use specific, distinct tags. The character browser combines selected tags with
**AND**, so selecting several tags shows only cards that have all of them.

## Tag cards

Choose **Judge classifier** or **Agent model**, then select **Tag N characters**,
review the warning, and confirm. Orb replaces each card's existing tags with
tags from the vocabulary.

The Judge asks about all vocabulary tags in one request per card and processes
cards concurrently. It applies a tag when the Judge gives it at least 80%
probability, keeping at most the 12 strongest matches. A card can have no
matching tags. Configure the Judge under **Endpoints → Judge** before choosing
this lane.

The Agent model handles cards one at a time and remains available when no Judge
endpoint is configured. It can also be useful when you prefer its tagging style.

Progress is saved after each card. You can cancel the run and continue later;
the next run processes only cards that still need tagging.

A card needs tagging when it is:

- new;
- edited after its last tagging; or
- affected by a newly added vocabulary tag; or
- tagged by an older version of Orb's classifier.

Reordering or changing the capitalization of the vocabulary does not require
another model run. Capitalization changes are applied to existing assignments.

When every card is current, the run button becomes **Retag all**. Use it after
changing the tagging lane or model, or when you want to repeat the Agent run
with **Tagger thinking** enabled. The pending count tracks card edits and
vocabulary changes, not which tagging lane you last used.

### Removing tags

Removing a vocabulary tag removes it from every card that Orb has auto-tagged.
This does not use the model, and Orb asks for confirmation before making the
change.

If you manually edit a card's tags after auto-tagging, that list becomes yours
again and vocabulary deletion does not prune it. The card remains eligible for
the next tagging run.

Adding the tag back later does not restore its old assignments. It makes the
cards eligible for tagging again.

### Tagger thinking

In the Agent lane, enable **Tagger thinking** for more deliberate results. It
takes longer and uses more model resources.

## Before you start

Auto-tagging replaces the card's existing tags, including imported or manually
entered tags. This cannot be undone in Orb.

Exports contain the new tags. Export a card before tagging if you want to keep
its original tags.

Editing a card after tagging marks it for tagging again, so manually entered tags
may be replaced by the next run.

## Model and failed cards

The Agent lane uses the Agent model. If you have configured a separate Agent
endpoint, Orb uses it; otherwise it uses the Writer endpoint. The Judge lane
uses the separately configured Judge endpoint and model.

Cards that return an unusable tagging answer remain unchanged and are tried
again the next time you run auto-tagging. Orb stops after five consecutive
unusable answers. Endpoint and network failures stop the run. The Agent lane
uses the endpoint's retry policy; the Judge lane may already have other cards
in flight when a failure arrives.
