# Compress History

Compress History summarizes an existing conversation and starts a new
conversation with the summary and the most recent messages. Use it when a chat is
getting too large or when you want a fresh context window.

## Create a compressed conversation

1. Open the conversation menu and select **Compress History**.
2. Choose how many recent messages to keep.
3. Add optional instructions, such as a preferred tense or details to omit.
4. Select **Generate**. **Stop** ends it early and keeps what was written.
5. Edit the summary by hand, or type a change under it and select **Revise**.
   **Regenerate** writes a fresh summary with the current instructions.
6. Select **Create New Conversation**.

Every Generate, Regenerate, or Revise adds a take; page between them with the
arrows above the summary, and hand edits stay on their take. Closing the dialog
keeps the takes until the conversation changes.

A take is written for the number of kept messages it was made with. Changing
**Keep last** afterwards disables **Create New Conversation** until you switch
back or regenerate.

Revise rewrites the current take, edits included, against the same history, so a
backend with a prompt cache only processes the new request.

## Titles

The original conversation stays unchanged. The new one takes its title with the
next part number: compressing "Lunch" creates "Lunch · Part 2", and compressing
that creates "Lunch · Part 3". The new conversation keeps the same character and
group cast, when applicable.
