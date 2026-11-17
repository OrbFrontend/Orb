# Assistant Chat

The assistant is an agent that works the app for you. Ask it in plain language
and it reads, creates, and changes characters, chats, lorebooks, fragments,
documents, and settings, renders pictures and speech, and searches the web. It
can do most things you can do from the UI, and nothing beyond it.

It runs on the **Agent** model, in a panel of its own. Its conversation is
separate from your story: nothing you say to it reaches a chat's prompt.

## Before you start

1. Open **Endpoints** and set up the **Agent** endpoint. The [Getting
   Started](../../getting-started.md#first-run) steps cover it.
2. Click **🤖 Assistant** in the chat header.

Any Agent endpoint works. One with native tool calling is faster: each step is
one model call instead of two. Larger models choose and fill tools more reliably;
turn on **Think** in the [options](options.md) for harder requests.

## Open the panel

**🤖 Assistant** is in the chat header, the document header, and the lorebook
drawer's header. On a narrow screen it is in the header's actions menu, and in
the document header as a speech-bubble icon.

The panel opens on the right, in place of the Tools panel or the Inspector, and
stays open as you move between chats, documents, and the library. On a narrow
screen it slides over the page; tap beside it or press Back to close it.

A request keeps running when you close the panel. The Assistant button shows a
mark while one runs and counts the changes waiting for your review.

## What it can do

| Area | It can | Try |
|---|---|---|
| Characters and personas | Read, create, and edit cards (description, greetings, tags…) and personas; switch the active persona; delete a character | "Tighten this character's description." |
| Chats and messages | Read and search a chat, edit or delete messages, start, rename, copy, and delete chats, find and show a branch, lock a persona to a chat; regenerate, rewrite, or continue a reply, or send your next message | "Help me find a lost chat branch." |
| Worlds and lore | Create worlds and entries, edit entries, set keywords and switches, attach a world to a chat, delete worlds and entries; list the entries in effect for a chat | "Update the active lorebooks based on how the story has progressed." |
| Fragments | Read, create, and edit mood and interactive fragments, including decisions | "Add a decision fragment: does she trust him yet?" |
| Settings and phrase bank | Read the pipeline switches; turn the Director, Output Auditor, Length Guard, Agentic Lorebook, and workflows on or off; set Length Guard limits; add and delete banned phrases | "Turn off the Director for now." |
| Documents | Read, create, and edit documents | "Tighten the passage I selected." |
| Images and speech | Create a picture, illustrate a reply, read a reply aloud | "Create an avatar image for this character." |
| Web | Search the web and read pages | "Look up this character's canon and fill in the card." |
| Library questions | Count, search, and compare across every chat, card, and entry | "What are the most used 2-grams in this chat?" |

Edits and deletes wait for you to review them; see [Reviewing
Changes](reviewing-changes.md). Everything else takes effect when the
assistant does it.

It cannot:

- see or change endpoints, models, API keys, avatars, or images on cards;
- delete personas, documents, or fragments (turn a fragment off instead);
- read pages that need JavaScript, or anything on `localhost` or your home network.

An empty panel suggests requests for the view you are in. Clicking one puts it
in the input; nothing is sent until you send it.

## What a request is about

Each request carries the view you sent it from, shown under the input:

| View | The request is about |
|---|---|
| A chat | That chat and its character. In a group chat, name the character you mean. |
| A character in the library | That character |
| A document | That document. Unsaved typing is saved before the request starts. |
| The lorebook drawer | The open world and the selected entry, on top of the view behind the drawer |
| The library | Your whole library |

**Select text** in a chat message or a document before you ask, and "this" means
that passage: "tighten this", "what does she mean here?". The start of the
selection shows under the input while it is attached. Clicking elsewhere on the
page drops it.

Changing view while a request runs does not change that request; the log shows a
divider where the view changes.

## Reading the log

The log shows your requests, the assistant's short notes as it works, one line
per tool it runs, and its answer. Click a tool line to see what the assistant
called it with, such as the query it ran or the page it read.

- **Stop**: the send button becomes Stop while a request runs. What the
  assistant already changed stays changed, and its last line says what.
- **Edit**: beside each of your requests. Rewording a request takes the
  assistant's conversation back to that point and sends it again. What later
  requests changed in your library stays changed; pending changes from them are
  discarded. The edit box says so before you send.
- **Retry**: on a request that was stopped, failed, or cut off, while it is the
  last one.
- **Jump to latest**: appears when you scroll up while the log grows.
- **Clear**: empties the conversation and starts a new one. It does not undo
  anything.

## Conversations and memory

Each character has its own assistant conversation, and each group chat has one.
Documents and the library share one. Turning to another character turns the
panel to that character's conversation.

The assistant remembers the whole conversation, including everything its tools
read, so a follow-up such as "now do the same for the other entry" works.
Recent conversations survive a reload and a restart. A very long conversation drops its
oldest requests; the log marks where the assistant's memory now starts.

It reads things when you ask, and does not see your edits as you make them. When
the chat behind the panel has moved on since the last request (new replies, a
regenerated reply, another branch), it is told.

## Limits

- One assistant request runs at a time across the app; one sent from another tab
  fails with "Another generation is already running". Story chats still
  generate alongside it.
- A request runs at most 12 tools. A bigger job takes more than one request: ask
  it to continue.
- A request is at most 8,000 characters, and a selection at most 20,000.
- A picture or speech render holds the assistant until it finishes.

## More

- [Reviewing Changes](reviewing-changes.md): what waits for you and how to apply,
  edit, or undo it.
- [Images and Speech](images-and-speech.md): pictures in the panel, reply
  illustrations, speech.
- [Options](options.md): Think, tool groups, and the system prompt.
- [Web Search](web-search.md): set up SearXNG.
