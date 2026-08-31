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
