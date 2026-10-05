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

Orb reuses the card sites' HTTP connections. Chub, Bernkastel, and Wyvern also
remember the page counts returned by recent searches, separately for each query
and sign-in. The bounded count cache expires after five minutes; each Randomize
click still fetches fresh results from the site. If a catalog shrinks and the
chosen page is empty, Orb retries within the new bounds or on the first page.
Wyvern needs an extra first-page lookup when it has no recent count.

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

## Randomize latency benchmark — 2026-10-05

The live guest benchmark used Python 3.11.15 on the development machine, five
clicks per source and query, with a two-second pause before each click. Each
query began with one normal browse. Sites ran in separate concurrent lanes,
with one request at a time per site. The table shows median successful backend
latency, including transport and normalization. It excludes browser rendering,
thumbnail downloads, signed-in catalogs, and card imports.

| Source | Query | Before | After | Reduction | Requests per successful click, before → after |
|---|---|---:|---:|---:|---|
| Chub | Empty | 0.753 s | 0.596 s | 21% | 1 → 1 |
| Chub | `elf` | 0.953 s | 0.383 s | 60% | 2 → 1 |
| Bernkastel | Empty | 0.901 s | 0.336 s | 63% | 1 → 1 |
| Bernkastel | `elf` | 0.830 s | 0.357 s | 57% | 1 → 1 |
| Botbooru | Empty | 1.092 s | 0.417 s | 62% | 1 → 1 |
| Botbooru | `elf` | 0.595 s | 0.506 s | 15% | 1 → 1 |
| Wyvern | Empty | 3.546 s | 1.421 s | 60% | 2 → 1 |
| Wyvern | `elf` | 3.806 s | 1.689 s | 56% | 2 → 1 |

Every successful warm request reused an existing connection after the change;
the original downloader opened a new connection for every request. Reusing a
scoped async client follows [HTTPX's connection-pooling guidance](https://www.python-httpx.org/async/#opening-and-closing-clients).
Wyvern no longer fetches page 1 on every click, and filtered Chub no longer
starts with a likely out-of-range random page. The same seed chose the same
pages before and after except for filtered Chub, whose new bounds change the
random sequence. These small samples describe this run; site and network
conditions vary.

Bernkastel's filtered run succeeded on four of five clicks in both versions.
An empty deep page triggered an immediate first-page fallback, which the site
refused with HTTP 429. A separate unpaced baseline encountered four 429 errors
across ten clicks. Connection reuse reduces latency but does not remove the
site's rate limits.

A separate single cold-click probe without a prior browse measured Wyvern at
4.921 s before and 4.309 s after, still with two requests. The largest savings
apply after a browse or the first Randomize click has supplied the page count.

Reproduce the warm and cold measurements from the project root:

```sh
.venv/bin/python scripts/benchmark_card_sources.py --output /tmp/card-source-latency.json
.venv/bin/python scripts/benchmark_card_sources.py --skip-browse --samples 1 --query ''
```

The script reads live public APIs without reading saved credentials or importing
cards. `--source`, `--query`, `--samples`, `--seed`, and `--interval` control the
run. [Recorded samples](../benchmarks/card-source-latency-2026-10-05.json) include
request timings, selected pages, status codes, response sizes, and new-connection
counts for the paced, cold, and unpaced baseline runs.
