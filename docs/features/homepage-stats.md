# Homepage Stats

When no conversation is open, the home screen shows a small grid of usage stats — a lightweight dashboard of how much you've written and who you've spent time with. It's refreshed every time you land on the home view.

## What's shown

The grid is built from `GET /api/stats` and includes:

- **Conversations** — total number of chats.
- **Messages** — messages on the currently visible (active) branch of every chat. Swiped-away regenerations don't count.
- **Words written** — everything *you* typed, across all branches (so the effort you put into discarded swipes still counts).
- **~Tokens generated** — a lifetime estimate of how much text the model has produced for you, divided by a `chars ÷ 4` heuristic.
- **Storage used** — the on-disk size of the database (including its WAL sidecars), shown when non-zero.
- **Avg response time** — average agent latency across logged turns.

Cards with a zero or missing value are hidden, so a fresh install stays uncluttered.

## The character spotlight

The hero slot is a portrait card highlighting one character as a little "story beat" rather than a bare number. The server picks one of these themes at random among those that have data:

- **★ Favorite character** — whoever has the most messages on their active branches.
- **💔 Misses you** — a well-worn character (over 100 messages) you *haven't* talked to in the last 24 hours, picked at random. The favorite is excluded so the two themes stay distinct.

If the character's card still exists, the whole spotlight is clickable and reopens it exactly as the library would.

## How the counters behave

- **Tokens generated** is a persistent lifetime counter, not a recount. It's seeded once from your existing assistant messages, then incremented on each successful turn — so deleting old conversations doesn't shrink it.
- **Message and favorite tallies** are computed live from the active branch of each chat, so they always reflect what you'd actually see if you opened the conversation.

## First run

The home screen normally greets new users with a "Select a character to begin" prompt. As soon as you have at least one conversation, that onboarding line is dropped and the stat grid takes over.
