# Compress History

Summarize chat context and move it to a new conversation — useful when context windows fill up or you want a fresh KV cache.

- The whole context save for the kept-turns gets summarized and used as the first message of the new conversation.
- The user chooses how many turns to keep, and prompt additional instructions for better summarization.
- The user may manually edit and regenerate if the summary isn't good enough.
