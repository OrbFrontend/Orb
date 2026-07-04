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
- **Prefill.** Orb can hand the model a partial response and have it only generate the rest, instead of regenerating text it already knows. See the Editor example below.

## Where it shows up

**Director** — each Scene Direction step now constrains the grammar to just the field being decided that step, so the model physically can't fill in fields it wasn't asked about yet (previously it just tended to anyway, despite being told not to, and Orb filtered the extras out after the fact).

**Editor** — the anti-slop/anti-repetition audit already knows the exact flagged sentence for each finding. Rather than one big call where the model re-prints every flagged sentence back as a `search` string (and sometimes gets it slightly wrong, breaking the patch), text mode issues one forced `editor_apply_patch` call per finding, prefilled up through `"replace": "`. The model only generates the replacement text, grammar-locked to a valid JSON string ending in the exact closing bytes. No re-typed search strings, no wasted tokens, no stale-search errors.

Both fall back to their classic, unforced-grammar behavior on chat-mode endpoints.
