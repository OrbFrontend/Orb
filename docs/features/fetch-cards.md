# Fetch Cards from the Internet

Orb can browse supported character-card repositories and open cards in the
character editor before you save them.

Open the character browser and select **Internet**.

## Supported sources

| Source | Format |
|---|---|
| **Chub** (`chub.ai`) | PNG cards |
| **Bernkastel** (`chararc.bernkastel.pictures`) | JSON cards and separate avatars |
| **Botbooru** (`botbooru.com`) | Tavern PNG cards |
| **Wyvern** (`wyvern.chat`) | JSON cards and merged embedded Worlds |

## Browse

Enter a search term and press Enter. Results show the card name, avatar, and
creator, with the site's tallies underneath: rating, downloads, favorites, or
chats, whichever two the site reports first. A site that reports none shows when
the card was last updated. Hover the avatar for the tagline, every tally, the
token count, and tags. Use **Load More** for another page.

Select **🎲 Randomize** for a random batch. Botbooru uses its own random ordering;
the other sources select a random page from the available catalog. Random results
are one batch, so **Load More** is hidden for that view.

## Site sign-in

Botbooru and Wyvern show guests only their SFW cards. When you select either one, a
sign-in row appears under the search bar. Sign in with an account on that site to
include exclusive cards in search and **🎲 Randomize** results. Botbooru asks for
the username, Wyvern for the account's email address. A Wyvern account made through
**Continue with Discord** or **Continue with Featherless** has no password, so it
cannot sign in from Orb.

Orb sends the password to the site once and stores only the session the site
returns. A Botbooru session lasts about 90 days. A Wyvern session lasts until the
password changes. When the site stops accepting the session, the row says the
sign-in has expired and results fall back to guest results until you sign in
again. **Sign out** forgets the session on this machine.

Preset exports leave out the session unless they include **Configs**, and
**Strip keys** removes it. Importing a preset never changes the sign-in on this
machine.

## Import

Select **Import** on a result. Orb downloads and parses the card, then opens the
character editor for preview. Nothing is added until you save it.

The card receives a stable ID. Importing the same card again updates the existing
character identity and keeps conversations linked to it instead of creating a
duplicate.
