# Text Completion Mode

An alternate connection mode for llama.cpp (and llama.cpp-compatible) endpoints. Instead of talking to the OpenAI-style `/v1/chat/completions` API, Orb renders the prompt itself and calls llama.cpp's native `/apply-template` + `/completion` endpoints directly. It's opt-in per endpoint and is faster and more reliable than chat mode wherever it's supported.

## Enabling it

In **Settings**, each endpoint (main and agent, configured separately) has an **API Mode** dropdown:

- **Chat Completions** — the default, OpenAI-compatible `/v1` API.
- **Text Completion (llama.cpp)** — the native transport described here.

Requires a llama.cpp server (or something that speaks the same `/apply-template` / `/completion` / `/props` endpoints). Conversations with images fall back to chat mode automatically on the same endpoint, since there's no multimodal render path yet — the cache stays warm either way.

## Why it's better

- **Cheaper prompt caching.** Chat mode has to serialize Orb's tool schemas into every prompt so the model knows what it can call. Text mode never puts tool schemas in the prompt at all — forced calls are constrained by a grammar instead — so the cached prefix is just the system prompt and chat history. That's a smaller, more stable prefix, which means more cache hits.
- **Grammar-constrained decoding.** When Orb forces a tool call, text mode compiles that tool's JSON schema into a grammar the model is decoding under, so it's structurally impossible for the model to produce broken JSON, a wrong field name, or an extra field. This all but eliminates the "model returned malformed tool call" error class.
- **Prefill.** Orb can hand the model a partial response and have it only generate the rest, instead of regenerating text it already knows. Reasoning prefill and Document mode's assisted generation both ride this.

## Where it shows up

**Director** — each Scene Direction step now constrains the grammar to just the field being decided that step, so the model physically can't fill in fields it wasn't asked about yet (previously it just tended to anyway, despite being told not to, and Orb filtered the extras out after the fact).

**Editor** — the forced `editor_apply_patch` call is grammar-locked to the tool's parameter schema, so the model physically cannot emit a malformed patch. Because a patch names its finding by id rather than by re-printing the flagged sentence, the generated JSON is a handful of integers plus the replacement prose — nothing the model could get subtly wrong. Text mode ran a per-finding prefill path for this before the id method landed; it is gone, and both transports now take the same single call.

The director falls back to its classic, unforced-grammar behavior on chat-mode endpoints.
