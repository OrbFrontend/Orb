# LLM Endpoint Routing

Orb keeps one OpenAI-shaped contract inside the pipeline: messages, tool calls,
stream events, terminal messages, and usage have the same shape regardless of
the provider. The inference layer resolves the configured URL and translates at
the network boundary.

## Accepted endpoint forms

A configured endpoint may be a versioned base or a full generation resource.

| Configured form | Generation resource |
|---|---|
| `https://host/v1` | `https://host/v1/chat/completions` |
| `https://host/v1/chat/completions` | Used exactly as entered |
| `https://host/v1/messages` | Used exactly as entered with Anthropic Messages |
| `https://api.anthropic.com` | `https://api.anthropic.com/v1/messages` |
| `https://generativelanguage.googleapis.com` | `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` |

An `anthropic` path segment is also a strong native-protocol hint. For example,
`https://gateway.example/providers/anthropic/v1` resolves to the sibling
`messages` resource. Full `chat/completions` and `messages` resource URLs are
authoritative even when their host would normally imply another protocol.

Model discovery uses the sibling `models` resource and the matching auth family.
OpenAI and Gemini routes use Bearer authentication. Native Anthropic routes use
`x-api-key` and `anthropic-version`. Extra headers may replace those defaults
case-insensitively.

## Automatic detection and probing

Official hosts and path hints are deterministic and do not probe. An ambiguous
custom URL preserves Orb's historical OpenAI request first. Only when the
pre-stream response body specifically identifies a route mismatch does Orb try,
on the same host, these conventional resources:

1. the configured base plus `chat/completions`;
2. host-root `/v1/chat/completions`;
3. host-root `/v1/messages`.

The HTTP status alone never starts probing: a 400 or 404 can describe a bad
model, schema, or tool choice rather than a bad route. Known request recovery
runs first. No route is changed after the first streamed delta, and local
text-completion calls do not enter this chat probing path.

Probing replays the complete POST, so a first request can upload the prompt up
to three times. Orb chooses that trade-off because native compatibility proxies
do not expose a reliable discovery contract. A successful protocol and path is
cached per configured URL and model for the life of the backend process;
configured settings are never rewritten.

A 401 or 403 can trigger one alternate Anthropic/Bearer auth attempt only when
the model name, path, or resolved protocol supplies Claude/Anthropic evidence.
This retry is independently bounded and never changes hosts.

## Provider request behavior

Native Anthropic requests are built from an allowlist. System messages are
hoisted; text, base64 images, tool calls, and tool results are translated to
Messages content blocks; adjacent roles are coalesced. Tool definitions use
`input_schema` and `strict: true`. OpenAI `extra_body` fields are not passed
through; only Anthropic-native `metadata` and `service_tier` are accepted from
that escape hatch. A missing `max_tokens` defaults to 4096.

Reasoning-on maps to adaptive thinking with summarized display, and supported
effort levels map to `output_config.effort`. Reasoning-off omits `thinking`.
Current Claude families that reject temperature, top-p, and top-k omit them;
unknown proxy model names try them once, learn from a specific rejection, and
omit them for later calls. `min_p`, repetition penalties, and logprobs are never
sent to Anthropic. Consequently, Document mode's per-token steering is not
available on native Anthropic endpoints.

Some routed models accept only `tool_choice="auto"`. This is distinct from a
provider that rejects `tool_choice` entirely: Orb rewrites `none`, `required`,
and named choices to `auto`, including the Writer's normal `none`. If the body
reveals this restriction for an unlisted model, Orb learns it for the process.
Director and Editor already handle a model declining the intended forced call.

Gemini uses Google's official OpenAI-compatible beta surface, Bearer auth, the
existing OpenAI stream parser, and strict structured output for forced calls.
Documented OpenAI fields, including `reasoning_effort`, remain intact. Native
Gemini features such as grounding and Files APIs are outside this version.

## Stable external contracts

Endpoint routing is internal and ephemeral. Public API and browser SSE shapes,
database settings, `LLMClient.complete()`, and `LLMClient.list_models()` do not
change. Anthropic stream events are translated to Orb's existing `content`,
`reasoning`, and terminal `done` events; provider error events inside an HTTP
200 stream use the same sanitized `LLMCallError` path as HTTP rejections.
