# Assistant Options

Open **Options** (the gear) in the panel's header. The gear is marked while
Think is on or a tool group is off. Every option applies from the next request.

## Think

Lets the model reason before each step: slower, but steadier on harder,
multi-step requests. Off by default.

## Tool groups

Turn off groups you don't use. The assistant then sees fewer tools, which makes
its prompt smaller and its choices simpler. Ask it for something a switched-off
group covers and it tells you which one to turn on.

| Group | Covers |
|---|---|
| Characters and personas | Cards and personas: read, create, edit, delete a character, switch persona |
| Chats and messages | Read, edit, and delete messages; start, copy, rename, delete, and branch chats; regenerate, rewrite, continue, send |
| Worlds and lore | Worlds and lore entries, and which worlds a chat uses |
| Fragments | Mood and interactive fragments |
| Settings and phrase bank | Pipeline switches, Length Guard limits, banned phrases |
| Documents | Read, create, and edit documents |
| Images and speech | Images, reply illustrations, speech |
| Web | Web search and page reading |

Library questions are always available. Pending changes can still be applied,
undone, or edited with any group off.

Keep the selection stable. Changing it, or Think, or the system prompt, makes
the next request process the whole conversation again.

## SearXNG URL

The search server the **Web** group uses. See [Web Search](web-search.md).

## System prompt

The opening of the assistant's instructions: who it is and how it talks. Write
your own to change its manner; **Reset** puts the default back. The rules and
tools that follow it stay the same. It saves when you click away from the box.
