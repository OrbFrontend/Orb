# LLM Endpoint Routing

Orb keeps one OpenAI-shaped contract inside the pipeline: messages, tool calls,
stream events, terminal messages, and usage have the same shape regardless of
the provider. The inference layer resolves the configured URL and translates at
the network boundary.

`core/llm_types.py` defines the normalized completion stream as a discriminated
`CompletionEvent` union: content and reasoning carry text deltas, token
probabilities carry their own records, and `done` carries a `CompletionMessage`
plus nullable provider usage. Chat, text, raw, and Claude Code transports, retry,
and cached calls preserve this contract. Reasoning relays retain the optional
call-boundary marker. The Writer consumes only content and reasoning; Document
mode also consumes probabilities and projects `done` to its finish reason.

Assembled message fields remain optional, including on an empty reply. Wire tool
arguments are JSON strings; `parse_tool_calls` returns `ParsedToolCall` records
with decoded JSON objects. Tool-owned arguments, usage extensions, and provider
reasoning details stay open. Raw provider frames are adapted before entering
the normalized stream. `CachedBase.complete_into` requires its label and tail
explicitly, yields reasoning, and fills a typed reply without adding absent
fields. An aborted call emits no assembled reply.

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

### Local server kinds

The image workflow's GPU swap option asks what kind of server each lane calls.
The probe never names a model, so it never loads one:

| Kind | Detected by | Unload |
|---|---|---|
| llama-server router (`--models-preset`, `--models-dir`) | `GET /props` answers `"role": "router"` | `POST /models/unload`, then poll `GET /models` until the entry leaves `loaded`/`loading` |
| llama-server single model (`-m`) | `Server: llama.cpp` with no router role | none, so the option stays hidden |
| llama-swap | `GET /running` answers a `running` list | `POST /api/models/unload/{id}`, then poll `GET /running` until the ID leaves |
| Ollama | `GET /api/version` answers a version and `GET /` answers `Ollama is running` | `POST /api/generate` with `keep_alive: 0`, then poll `GET /api/ps` |
| KoboldCpp | `GET /api/version` answers a version and `GET /api/extra/version` answers `"result": "KoboldCpp"` | none; the reason points at llama-swap |
| tabbyAPI | `GET /.well-known/serviceinfo` names `TabbyAPI`; `GET /v1/auth/permission` answers `admin` | `POST /v1/model/unload`; reload with `POST /v1/model/load` |
| text-generation-webui | `GET /v1/internal/model/info` answers `model_name` and `loader`; `GET /v1/internal/model/list` accepts the key | `POST /v1/internal/model/unload`; reload with `POST /v1/internal/model/load` |
| anything else | — | none |

Paths are relative to the server root, the base URL without `/v1`. A router's
`sleeping` status counts as unloaded. Ollama reports `name:tag`, so a bare
configured name compares as `name:latest`. A kind is cached per root for 30
seconds once a server answers, so a server restarted in another mode is noticed.
A failed model listing forgets it sooner.

KoboldCpp emulates Ollama's routes: its `/api/version` body is the same as
Ollama's, its `/api/ps` never drops the model, and its `/api/generate` runs a
real generation. The `GET /` check keeps Orb from sending it an Ollama unload.
A reverse proxy that rewrites Ollama's root makes Ollama read as `other`, which
hides the option.

With `apiKeys` set, llama-swap answers a call without the key with a 401 whose
body has `"src": "llama-swap"`, and Orb reports that the key can't unload.

llama-swap reports each process it has not stopped in `GET /running`, with
the state `starting`, `ready`, or `stopping`. Every listed process counts as
resident, because a stopping one still holds VRAM, but only `starting` and
`ready` count as a loaded model. `/running` names real model IDs, so a lane
configured with an alias is resolved through `GET /v1/models`, whose records
carry `meta.llamaswap.aliases`. A name that list leaves out (unlisted models,
`setParamsByID` variants, selectors) reads as not loaded and keeps no chat
cache. The unload answers once the process has stopped and cuts off that
model's in-flight requests, which is safe for Orb because a render waits for
its LLM calls first. Another app on the same llama-swap loses its request. A
render unloads every model llama-swap runs, like a router. A 404 from the
per-model unload route means a llama-swap too old to have it. llama-swap loads
a model again on the next request that names it, so Orb never reloads one. It
forwards the OpenAI routes and `/completion` but not `/apply-template`, so a
lane in text mode can't run through it.

tabbyAPI and text-generation-webui hold one model, answer the unload once it is
gone, and never load one back by themselves. A key that is not the admin key
reads as unable to unload. Before unloading, Orb reads what the server holds:
tabbyAPI's `GET /v1/model` card gives the load settings back, and
text-generation-webui's model info gives the model, loader, and LoRAs. The next
call on that server, after ComfyUI is freed, loads it again and waits for the
load to finish. A failed reload stays pending and is retried on the next call.

With **Keep the chat cache across swaps** on, a router target saves each slot
before its unload: `GET /slots?model=M&autoload=false` lists them, and
`POST /slots/{id}?action=save` with `{"model", "filename"}` writes one. The
next call for that model restores them with `action=restore` once ComfyUI has
freed its memory, which also loads the model. Each model and slot has one fixed
file name, so the files never outnumber slots times models. llama.cpp has no
delete call, and the files outlive the server process. A slot that fails to
save or restore costs only a prefill.

A llama-swap target uses the same slot calls through
`/upstream/{id}/slots`, the one llama-server that runs the model. Any request
there starts a stopped model, so Orb saves only models in the `ready` state.
The restore that follows a render cold-starts the process, so it waits up to
the 600-second load timeout instead of the 120-second slot timeout. The model's
`cmd` needs `--slot-save-path`.

#### ComfyUI hosted by llama-swap

ComfyUI counts as hosted when its URL has the same scheme, host, and port as a
llama-swap lane's root and its path is `/comfyui` (llama-swap's reserved
`comfyui_auto` model) or `/upstream/{id}`. Use the real model ID there, not an
alias. `/running` reports real IDs, so with an alias Orb can't tell ComfyUI
apart from a chat model: it reports `starting` before every render and tries,
harmlessly, to save ComfyUI's slots. llama-swap then evicts models for ComfyUI
and the other way around, following its groups, so Orb changes two steps:

- **Before a render**, Orb waits as usual and, with the chat cache option on,
  saves the slots of `ready` models. It does not unload them. If ComfyUI is
  not `ready`, Orb sends `GET {comfy}/system_stats` with the load timeout and
  reports the `starting` stage. That request makes llama-swap start ComfyUI and
  evict what its groups say to, so the render's own calls hit a warm server.
  A group that lets a small model run beside ComfyUI is respected.
- **Before the next LLM call**, Orb never sends ComfyUI's `/free`, because any
  request but a few static `GET`s starts a stopped ComfyUI. The model's own
  request, or the first slot restore, makes llama-swap evict ComfyUI. An LLM
  lane on another server stops ComfyUI with llama-swap's unload instead.

The arbiter is still needed: llama-swap counts only HTTP requests as in
flight, and ComfyUI's `POST /prompt` returns at once, so without it a reply
during a render would evict ComfyUI and kill the render. The image settings'
connection test and model lists reach ComfyUI outside the arbiter, so with
ComfyUI hosted they start it and evict the chat model, which loses its cache.

## Lane presets

Each lane sends the sampler preset of the endpoint it is calling. Director,
Editor, and workflow tool calls read the Agent model config's temperature,
budget, and samplers; they fall back to the Writer's whenever the Agent lane does
not resolve, which is what one endpoint serving both lanes means.

Calls built from a lane preset send its numeric **Max Tokens** value unchanged. A
cleared value is omitted so the provider applies its own default. No normal pass
raises or lowers a numeric configured budget, so the number in settings is the
number on the wire. A forced tool call has to fit its whole answer in that budget.
A reply cut off at it is never accepted as a complete answer.
Features that report errors name the setting to raise: *Agent Max Tokens* when a
separate Agent lane resolves, otherwise *Max Tokens*. The document Output Auditor
patches on the Writer endpoint to keep byte parity with the prompt that generated
the draft, so it spends the Writer's budget.

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
capability evidence. In a model config, a number sends that parameter and an
explicit `null` omits it, leaving the provider default in control; a missing
field in a PATCH means no configuration change. The settings UI represents
`null` as a cleared numeric input. `min_p`, repetition penalties, and logprobs are never sent
to Anthropic. Consequently, Document mode's per-token steering is not available
on native Anthropic endpoints.

Some routed models accept only `tool_choice="auto"`. This is distinct from a
provider that rejects `tool_choice` entirely: Orb rewrites `none`, `required`,
and named choices to `auto`, including the Writer's normal `none`. If the body
reveals this restriction for an unlisted model, Orb learns it for the process.
Director and Editor already handle a model declining the intended forced call.

A forced call carried as strict structured output sends the tool's parameters as
its schema, normalized first: every property becomes required (optional ones
nullable), objects are closed, and validation keywords the strict subset
excludes — `uniqueItems`, `contains`, `propertyNames`, and the rest — are
dropped. Dropping them only widens what a valid answer may contain, whereas
leaving one in fails the entire request: NanoGPT answers HTTP 400 naming no
field. Endpoints that receive a `tools` array get the schema with those
keywords intact.

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

Text Completion calls on a llama-server router name the model on every native
route: `/props?model=`, and `model` in the `/apply-template` and `/completion`
bodies. A router rejects a body without it and loads the named model when it is
not resident. A single-model server ignores the field.

## Stable external contracts

Endpoint routing is internal and ephemeral. Public API and browser SSE shapes,
database settings, `LLMClient.complete()`, and `LLMClient.list_models()` do not
change. Anthropic stream events are translated to Orb's existing `content`,
`reasoning`, and terminal `done` events. Provider error events inside an HTTP
200 stream use the same sanitized `LLMCallError` path as HTTP rejections on
both Anthropic and OpenAI-compatible routes. An OpenAI-compatible stream must
reach a finish reason or `[DONE]`; an earlier EOF is a provider failure unless
the turn was stopped.

## Experimental Claude Code transport

The endpoint marker is `claude-code://local`. It is saved as an ordinary chat
endpoint, so the Writer and Agent lanes, and single-model mode, can each select
it from the saved endpoint list. Selecting it on the Writer lane shows a setup
card that checks the `claude` CLI and its existing login on the machine hosting
Orb. If needed, run `claude auth login --claudeai` there. Claude Code
subscription availability, limits, and billing depend on Anthropic; this
integration is unofficial and may go away in the future.

### Inspector mood availability

`GET /api/conversations/{cid}/messages/{msg_id}/director-log` includes
`mood_data_available`. It is false when the reply has no saved diagnostic log;
its compatibility `active_moods: []` must not be interpreted as a known inactive
mood selection. Inspector badges describe the selected reply. Resting badges use
the cooldown snapshot before that reply (before the exchange for group replies),
never the cooldowns that the reply leaves for the next turn.
