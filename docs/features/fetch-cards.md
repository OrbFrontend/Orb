# Fetch Cards from Internet

Browse and import character cards directly from online repositories, without leaving Orb.

Open the character browser and switch to the **🌐 Internet** view. From there you can search a source, page through results, or hit **🎲 Randomize** for a fresh selection, then **Import** any card.

## Sources

| Source | Notes |
|---|---|
| **CharacterHub** (chub.ai) | Cards are served as `chara_card_v2` PNGs from the CDN and parsed like a file import. |
| **Character Archive** (chararc.bernkastel.pictures) | Mirrors cards from upstream sites; the definition is served as V2 JSON and the avatar is fetched separately. |
| **Botbooru** (botbooru.com) | Serves standard tavern PNG cards (tEXt `chara` chunk), parsed like a file import. Has a native server-side random sort. |
| **Wyvern** (wyvern.chat) | Uses an unauthenticated JSON explore API; the definition is served as V2 JSON. Embedded lorebooks are merged into a single V2 `character_book`. |

Requests are proxied through the Orb backend (the `/api/characters/browse`, `/randomize`, and `/import-url` routes) so browser CORS restrictions don't get in the way. Sources are registered behind a small registry, so new ones can be added without touching the UI.

## Browsing

- **Search** — type a query and press Enter. Results show name, avatar, and tagline, with **Load More** for pagination.
- **Randomize** — Botbooru has a native random sort, so it returns a fresh random batch directly. The other sources have no random sort, so Orb jumps to a random page of the (optionally query-filtered) catalog instead. Either way it's a one-shot batch; "Load More" is hidden because paging would silently revert to ranked order.

## Importing

Clicking **Import** downloads and parses the card through the same `tavern_cards` pipeline as a local file import, then opens it in the **character editor** so you can review and tweak it before saving — nothing is added to your library until you confirm.

Each imported card gets a stable id (derived from the embedded Orb id, the card bytes, or its path), so re-importing the same card relinks any existing conversation history rather than creating a duplicate.
