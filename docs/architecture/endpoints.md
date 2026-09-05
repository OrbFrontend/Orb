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
| `https://host/v1beta/openai` | `https://host/v1beta/openai/chat/completions` |

Full `chat/completions` and `messages` resource URLs are authoritative. Bare and
versioned bases remain ambiguous: provider names in a hostname or path, and
family names in a model id, never select a protocol.

The `/v1beta/openai` compatibility dialect is likewise recognized from its
resource path, not its hostname. Any proxy or gateway exposing that shape takes
the same request policy, reasoning translation, and catalogue normalization.

Model discovery uses the sibling `models` resource and the matching auth family.
For an ambiguous base it walks the same route candidates until a catalogue
satisfies the shared `data[].id` contract, then caches that route for generation.
OpenAI and Gemini routes use Bearer authentication. Native Anthropic routes use
`x-api-key` and `anthropic-version`. Extra headers may replace those defaults
case-insensitively.

## Automatic detection and probing

Explicit resource shapes are deterministic and do not probe. An ambiguous URL
preserves Orb's historical OpenAI request first. Only when the
pre-stream response body specifically identifies a route mismatch does Orb try,
on the same host, these candidate resources:

1. the configured base plus `chat/completions`;
2. the configured base plus `messages`;
3. host-root `/v1/chat/completions`;
4. host-root `/v1/messages`;
5. host-root `/v1beta/openai/chat/completions`.

The last is the beta OpenAI compatibility resource, which some proxies expose
at their bare root and the two `/v1` guesses cannot reach.

The HTTP status alone never starts probing: a 400 or 404 can describe a bad
model, schema, or tool choice rather than a bad route. Known request recovery
runs first. No route is changed after the first streamed delta, and local
text-completion calls do not enter this chat probing path.

Probing replays the complete POST, so a first request can upload the prompt up
to five times. Orb chooses that trade-off because native compatibility proxies
do not expose a reliable discovery contract. A successful protocol and path is
cached per configured URL and model for the life of the backend process;
configured settings are never rewritten.

A rejected `reasoning_effort` value is recovered on any OpenAI-protocol route:
Orb offers a superset of levels (`xhigh` is not a Gemini value), so a body that
names the field as invalid drops it for one retry and for the rest of the
session. Providers' accepted sets move, so this is learned from the response
rather than held as a per-provider list.

A 401 or 403 can trigger one alternate native/Bearer auth attempt for a Messages
route or an ambiguous route. An explicit OpenAI resource only retries when the
response names the native `x-api-key` header. This retry is independently
bounded and never changes hosts; provider and model names are not evidence.

## Provider request behavior

Native Anthropic requests are built from an allowlist. System messages are
hoisted; text, base64 images, tool calls, and tool results are translated to
Messages content blocks; adjacent roles are coalesced. Tool definitions use
`input_schema` and `strict: true`. OpenAI `extra_body` fields are not passed
through; only Anthropic-native `metadata` and `service_tier` are accepted from
that escape hatch. A missing `max_tokens` defaults to 4096.

Reasoning-on maps to adaptive thinking with summarized display, and supported
effort levels map to `output_config.effort`. Reasoning-off omits `thinking`.
Sampling controls are sent optimistically. A specific rejection teaches Orb to
omit them for later calls to that endpoint/model pair; names never stand in for
capability evidence. `min_p`, repetition penalties, and logprobs are never sent
to Anthropic. Consequently, Document mode's per-token steering is not available
on native Anthropic endpoints.

Some routed models accept only `tool_choice="auto"`. This is distinct from a
provider that rejects `tool_choice` entirely: Orb rewrites `none`, `required`,
and named choices to `auto`, including the Writer's normal `none`. If the body
reveals this restriction for an unlisted model, Orb learns it for the process.
Director and Editor already handle a model declining the intended forced call.

Gemini uses Google's official OpenAI-compatible beta surface, Bearer auth, the
existing OpenAI stream parser, and strict structured output for forced calls.
Documented OpenAI fields, including `reasoning_effort`, remain intact. Native
Gemini features such as grounding and Files APIs are outside this version.

Structured output is what carries a forced call, so `tools` and `tool_choice`
are withheld from *every* Gemini request, not only the forced ones — the
argument-fidelity and prefix-stability reasons are the same ones set out for
any structured-output endpoint above. A pass that offers tools under
`tool_choice="auto"` therefore has none on the wire and answers as prose; the
Editor's unforced iteration is the one such pass, and it stops as it would for
any model that declined to call a tool.

Reasoning-off is translated. Orb's `reasoning`, `chat_template_kwargs`, and
`thinking` fields mean nothing to the compatibility layer and are silently
ignored, so reasoning-off calls carry `reasoning_effort: "none"` — the control
the layer actually reads. Families that cannot disable thinking reject that
value; the rejection is learned per model like any other capability fact.

`logprobs` is not supported on this surface, so Document mode's per-token
steering is unavailable on Gemini for the same reason it is on Anthropic.

## Stable external contracts

Endpoint routing is internal and ephemeral. Public API and browser SSE shapes,
database settings, `LLMClient.complete()`, and `LLMClient.list_models()` do not
change. Anthropic stream events are translated to Orb's existing `content`,
`reasoning`, and terminal `done` events; provider error events inside an HTTP
200 stream use the same sanitized `LLMCallError` path as HTTP rejections.
