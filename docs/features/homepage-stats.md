# Homepage Stats

When no conversation is open, the home screen shows usage totals. The values
refresh whenever you return to the home screen.

## Available totals

- **Conversations**: total chats
- **Messages**: messages on the active branch of each chat
- **Words written**: your words across all branches
- **~Tokens generated**: an estimate of model output using characters divided by four
- **Storage used**: database size, including its SQLite WAL files
- **Avg response time**: average latency for logged turns

Empty or zero-value cards stay hidden.

## Character spotlight

The spotlight can show:

- **Favorite character**: the character with the most messages on active branches
- **Misses you**: a character with more than 100 messages that you have not used in
  the last 24 hours

The spotlight is clickable when the character still exists.

The token total is a lifetime counter. Message and favorite counts are calculated
from current active branches, so discarded message branches do not count there.
