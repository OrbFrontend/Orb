# Text Completion Mode

Text Completion Mode uses llama.cpp's native `/apply-template`, `/completion`,
and `/props` endpoints instead of the OpenAI-compatible chat API. Enable it per
endpoint when the server supports these routes.

## Enable it

In **Settings**, set an endpoint's **API Mode** to:

- **Chat Completions**: the default OpenAI-compatible transport
- **Text Completion (llama.cpp)**: the native llama.cpp transport

Use a llama.cpp server or a compatible implementation. Conversations with images
fall back to Chat Completions on the same endpoint because Text Completion Mode
does not yet have a multimodal path.

## Why use it

- Tool schemas stay out of the prompt, which can reduce prompt size and improve
  cache reuse.
- Grammar-constrained decoding keeps forced JSON tool calls within their schema.
- Prefill lets Orb provide part of a response and generate only the remainder.

The mode applies to Director and Editor calls as well as normal writing. Director
steps constrain each fragment to the field being filled. The Editor's rewrite
patches use the same schema constraints.

## Reasoning formats

Orb learns reasoning boundaries from four small `/apply-template` renders:
thinking on/off, and an assistant message with a synthetic reasoning field that
is either populated or empty. These probes generate no tokens and contain no
conversation data. They support paired reasoning tags (including namespaces),
Gemma's thought channel, and Muse Glimmer's messages addressed to `self`/`user`.
Raw Jinja text is used only to detect template changes, never as evidence for
reasoning markers or prompt-control bytes.

Each client reuses a successfully discovered profile while the server's
`chat_template` stays the same. Metadata is checked on every call and again
after discovery, so a missing `/props` response cannot reuse stale assumptions.
Missing metadata, ambiguous renders, or unsupported reasoning boundaries stop
before generation. Check the server and retry, or select Chat Completions mode.
A template that renders identically with and without reasoning is handled as an
unstructured text template; this describes the template, not the model's latent
ability to reason.

The final prompt carries an explicit continuation state. Assistant prefills are
content; reasoning prefills are reasoning. Grammar-constrained calls select the
reply using the observed template transitions, even if the template ignored a
reasoning-off request. These adjustments touch only the prompt tail, preserving
the common system/history prefix. A reasoning prefill explicitly continues the
thought instead; it cannot be inserted after a prompt already selects a reply.
