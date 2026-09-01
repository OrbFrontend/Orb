# Fetch Cards from the Internet

Orb can browse supported character-card repositories and open cards in the
character editor before you save them.

Open the character browser and select **🌐 Internet**.

## Supported sources

| Source | Format |
|---|---|
| **CharacterHub** (`chub.ai`) | PNG cards |
| **Character Archive** (`chararc.bernkastel.pictures`) | JSON cards and separate avatars |
| **Botbooru** (`botbooru.com`) | Tavern PNG cards |
| **Wyvern** (`wyvern.chat`) | JSON cards and merged embedded Worlds |

## Browse

Enter a search term and press Enter. Results show the card name, avatar, and
tagline. Use **Load More** for another page.

Select **🎲 Randomize** for a random batch. Botbooru uses its own random ordering;
the other sources select a random page from the available catalog. Random results
are one batch, so **Load More** is hidden for that view.

## Import

Select **Import** on a result. Orb downloads and parses the card, then opens the
character editor for preview. Nothing is added until you save it.

The card receives a stable ID. Importing the same card again updates the existing
character identity and keeps conversations linked to it instead of creating a
duplicate.
