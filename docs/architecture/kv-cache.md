# How Orb Reuses the LLM's KV Cache

This doc explains, in plain English, how Orb keeps each LLM call fast and cheap by carefully reusing the **KV cache** — the working memory the model builds up as it reads a prompt.

Audience: someone who can read code but isn't deep in LLM internals. No tokenisation math, no transformer diagrams.

> **Animation:** [kv-cache-animation.html](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html) is a stepped, self-contained walkthrough of the mechanism across all three passes and two turns — and of the reasoning-mode fork that silently splits the cache when `reasoning_enabled_passes` differs across passes (it walks through a director-on, writer/editor-off configuration; the shipped default is all three off, so no fork out of the box — see §9). Open it in a browser.

---

## 1. What is a KV cache, in one paragraph

When an LLM reads a prompt, it builds an internal scratchpad — the "KV cache" — token by token. If the next prompt **starts with the exact same text** as the previous one, the inference server can skip rebuilding that part of the scratchpad and pick up where it left off. The cache only works as a **prefix**: matching has to start from character zero. Change one comma near the top, and everything after it has to be redone. Append new text at the bottom — the saved work is still good.

That's the only rule Orb cares about, and the whole architecture below is built around honouring it.

---

## 2. The "stack of pancakes" mental model

Picture every LLM call as a stack of text pancakes:

```
┌──────────────────────────────┐
│  trailing instruction        │  ← varies per pass
├──────────────────────────────┤
│  chat history (oldest→newest)│  ← identical across passes
├──────────────────────────────┤
│  system prompt               │  ← identical across passes
└──────────────────────────────┘     (bottom of the stack = start of the prompt)
```

Orb's golden rule: **keep the bottom of the stack identical, only change the top.**

Every pass (director, writer, editor) sends the same system prompt and the same chat history. They only differ in the final pancake — the trailing user message and which tool the model is forced to call. The shared bottom is computed once per turn and handed unchanged to every pass.

---

## 3. The three passes side by side

Within a single turn, Orb makes 2–4+ LLM calls. Here's what each one looks like:

### Director pass (1–2 calls)

```
  system + history                ← cached prefix
+ "[OOC: pause to enhance...] "
+ "Call ONLY this tool: direct_scene ..."
+ user's actual message
```

`tool_choice` is forced to `direct_scene`. The model returns a tool call, never raw prose.

### Writer pass (1 call)

```
  system + history                ← cached prefix (same as director's)
+ lorebook block                  ← keyword hits + Director picks only; constants sit in the system prompt
+ "**Scene Direction**\n<Mood content>"   ← injected from director's output
+ user's actual message
+ lorebook depth block            ← `constant` + `at_depth` entries (SillyTavern's @ Depth)
```

`tool_choice="none"`. The model writes prose. On most backends this also deletes the tool schemas from the rendered prompt, which is why the writer rarely shares a prefix with the director — see Invariant 3.

### Editor pass (1–3 calls, only if needed)

```
  system + history                ← cached prefix (same as writer's)
+ writer's exact user message     ← reuses the writer's trailing pancake
+ assistant: <writer's draft>     ← the prose the writer just produced
+ "[OOC: you are the editor...] Apply patches to fix: <numbered audit report>"
```

`tool_choice` is forced to `editor_apply_patch` or `editor_rewrite`.

The editor's prompt **extends** the writer's prompt: the writer's trailing user message is reused verbatim. So the editor's cached prefix isn't just system + history; it's all of that **plus the writer's pancake plus the writer's draft**. That's the bulk of where the editor's savings come from.

Findings in the report are numbered, and `editor_apply_patch` takes `{id, replace}` — the model never re-prints draft text. The valid id set changes every turn, so it is stated in prose and validated server-side; expressing it as a per-turn `schema_overrides` entry would bust the shared prefix every turn for every pass, which is exactly what Invariant 3 forbids.

---

## 4. The invariants that keep the cache intact

### Invariant 1 — One system prompt, shared by every pass

Character card, persona, constant lorebook entries (the `## Lorebook` section, rendered right after the character description), scenario, example dialogue, post-history instructions, and user description are concatenated into a single system message once per turn. The same string is sent to all three passes. No pass adds, edits, or reorders anything.

Inline macros in these fields are resolved during that per-turn build, so they must be byte-stable across turns: `{{random}}` is — it resolves through a per-conversation seed (`Macros.seed`: the conversation id, or the carried `conversations.macro_seed` on checkpoint/compress copies so picks match the copied history), always yielding the same pick — but `{{roll}}` re-rolls every turn and will silently change the system-prompt bytes; avoid `{{roll}}` in card prefix fields and constant lorebook entry names/content. A constant entry that *needs* fresh rolls sets `at_depth` instead — that moves it out of the prefix into the per-turn tail, where it is resolved unseeded and costs no cache (see the writer diagram above).

Constant lorebook entries are the deliberate mirror image of Invariant 4: they are byte-identical every turn (canonical `priority DESC, sort_order, id` sort in `render_lorebook_block` keeps the section stable regardless of query order), so they live here in the cached prefix rather than re-billing in the trailing block of every director *and* writer call. The flip side: editing a constant entry, toggling its `constant`/`enabled` flag, or disabling its world changes prefix bytes and re-bills from that point on the next turn — the same class of (rare, accepted) cache bust as editing the card itself. Keyword-triggered and Director-picked entries change per turn and stay in the trailing block, as do constant entries flagged `at_depth`, which opt out of the prefix in exchange for per-turn macro resolution.

### Invariant 2 — One history list, shared by every pass

The chat history is built once per turn. Each pass receives the same list. Attachments (images) are encoded with the same bytes on every reference.

In group mode the assistant history is member-labelled, with consecutive
text-only assistant rows merged into one chat message. The Director runs against
the pre-exchange base once, speaker 2 and later rebuild a base over the now-grown
history, and pre-pipeline workflow system blocks are captured once and reapplied
to every later-speaker rebuild.

*What* the system body says about the cast is the scene's **character context
mode** ([Group Chats](../architecture/group-chats.md)), and the three modes have
three different cache topologies:

| Mode | Director prefix | Writer/Editor prefix | Cache consequence |
|---|---|---|---|
| Private perspective | shared public-cast prefix | the same prefix | One common trunk. The speaking card is fresh *after* history, so it is a bounded tail miss — this is why an alternating cast still reuses the long conversation body. |
| Shared dossier | shared-dossier prefix | the same prefix | Best prefix sharing across speakers, but every call carries every dossier's tokens. Its cold prompt is the sum of the cast. |
| Classic card swap | neutral public-cast prefix | the same prefix + the active card | One cache lane per speaker, and the Director cannot prewarm a selected speaker's Writer prefix. |

The public-cast block is rendered without reference to the speaker, so the
Director's neutral prefix is a byte-prefix of every speaker's: Swap adds the
active card *after* it, it does not replace it.

Two consequences of Swap are easy to get wrong. The swap sits *before* history,
so changing speaker invalidates the history KV too, not just the card region —
a one-prefix local server loses most of its history hit whenever the cast
alternates, while a provider that retains several prefix branches may keep one
warm lineage per character. And because the Director runs before a speaking plan
exists, it must use the neutral base: `_generate_group_exchange`'s `index == 0`
shortcut, which hands speaker 1 the setup base verbatim, is a *correctness* bug
under Swap rather than a cache miss, so that mode rebuilds for the first speaker
too (`prefix_is_speaker_scoped`).

Nothing about *which* members are in the scene reaches the tools blob. The
group `direct_scene` override widens the schema with a `speaking_plan` field
whose description names the field and not the cast; the castable keys ride the
Director's trailing request (`director.speaking_plan_instruction`), because
muting a member is otherwise prefix-neutral — a muted member still renders in
the cast section — and a schema that listed the unmuted keys made one mute
toggle a whole-prefix miss on every lane. Same rule as the editor's finding
ids: volatile list in prose on the tail, validated server-side.

Message content upholds Invariant 2 in a group the same way it does across
turns, and the exchange driver is the one place that needs care: the reply a
later speaker reads is assembled in memory from what the previous speaker just
wrote, so its inline macros must be the *same* resolution the row was persisted
with. `_persist_result` resolves once and writes the result back onto the turn
state that the `speaker_done` event carries; resolving a second time downstream
would re-roll and leave the in-exchange history disagreeing with every later
read of that row.

In Private and Shared a speaker's identity fields live only in the trailing
Writer message or only in the shared body respectively — never both, so no mode
bills the same card text twice. Both model lanes are built under the same mode
and the same speaker, or the Editor's agent lane would audit a draft written
against a different cast. A mode change is intentionally a cache miss on the
next request.

### Invariant 3 — One tool list, shared by every pass

Inference servers serialise the tool schema list into the cached prefix (where in the chat template depends on the server, but it's always *inside* the cached region). So the tool list has to be byte-identical across passes — including passes that won't call any tool. Every pass sends the same schemas; passes that aren't allowed to call them set `tool_choice="none"`.

That is the part Orb controls. Whether it produces an identical *prefix* is the server's call — and usually it doesn't.

#### `tool_choice` rewrites the prompt

Measured 2026-08-04. Same messages every call, only the tool params varied, reading `prompt_tokens` back.

| `tool_choice` | DeepSeek v4-pro | Gemma-4-26B (Ionstream) | Gemma-4-31B (CoreWeave) | GPT-4.1-mini |
|---|---|---|---|---|
| omitted / `auto` | all 6 | all 6 | all 6 | all 6 |
| `required` | all 6 | all 6 | all 6 | all 6 |
| `"none"` | **nothing** | **nothing** | **nothing** | all 6 |
| forced *X* | **only X** | **only X** | all 6 | all 6 |

Where the table says "nothing", the token count equalled a request with no `tools` field *exactly*: 6339 on DeepSeek, 6723 on both Gemmas. Not close — equal.

So a chat-mode turn renders three different prompts. The director's carries one forced schema, the writer's carries none, the editor's carries a different forced schema. `CachedBase` is doing its job. The prefix diverges anyway.

**One cache, not one per schema set.** Warm a prefix with no `tools` field, then resend the same messages with all six schemas and `tool_choice="none"`: 99–100% hit. Schemas are only text in the prompt. `tool_choice` decides whether that text exists; it never enters the cache key.

**Think in lanes, not turns.** Each distinct rendering is its own cache lineage — a *lane*. A pass reuses **its own** previous call, across turns; it does not reuse its siblings within a turn. So this is a cold-start cost paid once per lane, not a recurring per-turn tax.

Cold start, every lane empty, messages held identical across passes, counting freshly-prefilled prompt tokens:

| turn shape | DeepSeek v4-pro | Gemma-4-26B (Ionstream) |
|---|---|---|
| shipped (forced → `none` → forced) | 13 722 | 7 614 |
| all passes `auto` | 7 276 | 7 368 |
| no `tools` on any pass | 6 479 | 6 732 |

The conversation was ~6 300 tokens. Once those lanes are warm, the same three passes cost **760** (DeepSeek) and **673** (Ionstream) on a ~6 900-token conversation — both figures reproduced to the token across two runs. The shipped shape is expensive to start, not expensive to run.

One measured asymmetry explains why the warm numbers are that good. A `none` call does not inherit from an `auto` call on DeepSeek — 0%, reproduced at 2 s, 15 s and 45 s gaps, so not commit latency. But a forced call *does* inherit from a `none` call, at 90%. Orb's pipeline never sends `auto`: director and editor force, the writer sends `none`. So the passes do share the conversation body, and only the schema block at the tail is re-prefilled.

**Lanes multiply with workflows.** Image generation adds two more — `analyze_scene` and `compose_image_prompt`, each forced, each rendering its own schema. With all three passes on and scene analysis enabled a conversation carries five warm prefixes instead of one. Measured across `send message → gen image → gen message → gen image → regen image`: 18 606 fresh tokens on DeepSeek, 17 514 on Ionstream — of which the first two calls, both cold, are 13 018 and 13 549. Everything after the cold start ran warm, and the image regen cost 141 tokens on DeepSeek, 44 on Ionstream.

**Interleaved image calls do cost the Director.** Running the same two turns with the image steps removed, the Director's turn-2 call drops from 733 to 222 fresh tokens on DeepSeek and from 615 to 232 on Ionstream; the writer and editor are unchanged to within a token. So an image generation between turns costs the next Director roughly 380–510 tokens — it matches at the boundary the image lane established rather than extending its own previous call. Both arms reproduced exactly across two runs on DeepSeek. A full three-pass turn therefore costs 760 (DeepSeek) / 673 (Ionstream) on its own, or 1 269 / 1 054 with an image generation in between. More lanes means more cold starts, more prefixes competing for residency, and this one bounded per-turn tax on the Director — not a bigger bill per call.

`OFFER_TOOLS` does not buy what its comment in `workflows/_forced_call.py` claims. The idea is that shipping the identical two-tool blob on both calls lets them share a prefix *including* the blob. On backends that narrow the render to the forced tool they share only the conversation body: forcing `compose_image_prompt` after `analyze_scene` reuses 6 016 of 6 722 tokens on DeepSeek and 6 400 of 6 880 on Ionstream, both below the tools-free body length. The loss is bounded to the blob — a few hundred tokens per image — so the array is worth keeping for providers where it does work, but §9's "they reuse one another" is true only of the conversation, not the schemas.

**Both obvious repairs fail.** Giving the writer `tool_choice="auto"` restores the shared prefix and breaks the writer — 9 of 9 calls answered with a `direct_scene` tool call instead of prose. `structured_tool_calls` (below) is the only shape where every pass provably renders alike, but DeepSeek rejects its prerequisite: strict `response_format: json_schema` returns 400.

So `"none"` stays. It is load-bearing, not incidental. What changes is the expectation — a shared `CachedBase` does not imply a shared prefix, and the cost is a per-endpoint fact to measure rather than assume. Text mode sidesteps all of it (see the end of this section).

**Structured-output endpoints send no schemas at all -- on every pass.** Where an endpoint profile sets `structured_tool_calls` (`backend/inference/endpoint_profiles.py`), a forced pass constrains output with a strict `response_format` schema instead of a forced `tool_choice`, and `_complete_chat` then omits `tools` from the body for *every* pass on that endpoint -- along with the accompanying `tool_choice` (the writer's `"none"` included: with no tools in the body there is nothing to choose from). What matters for this invariant is that the list is identical across passes, not that it is non-empty, so an endpoint-wide omission satisfies it the same way an endpoint-wide blob does -- and it caches better, because the prefix is system + history only. Omitting the blob only on the forced passes would break the invariant: the writer would render a prefix with tools while the director and editor rendered one without. The blob is still assembled and still threaded through `CachedBase.tools` and the KV tracker's local accounting; it is the source of the `response_format` schema, it just never reaches the wire. Correctness depends on this too, not only caching -- a model that can still see `tools` may answer with a native tool call, which bypasses the schema entirely and, on some models, comes back with the argument keys rewritten.

**The opt-in is optimistic, and evidence can revoke it.** `structured_tool_calls` is one knob per endpoint, but a proxy endpoint (NanoGPT) fronts a different upstream engine per model id, and honoring strict `response_format` is that engine's property, not the proxy's. A route that *rejects* the field answers 4xx and self-heals through `recover_from_error`; one that accepts and silently ignores it returns 200 with free-decoded prose, the synthesized arguments fail to parse, and the pass contributes nothing -- every turn, invisibly, until the process restarts. Observed on `stealth/ox-alpha` via NanoGPT, which answered a forced `direct_scene` with a `<memo>`-wrapped prose plan. So `_complete_chat` reads the reply: a completed, non-empty answer that is not the object the schema demanded is proof the constraint was absent, and `note_structured_output_ignored` demotes that `(endpoint, model)` pair for the rest of the process. Nothing weaker counts as proof -- an empty reply or a `length` cutoff stays silent, the same standard `note_forced_tool_choice_ignored` holds itself to, because the demotion costs the endpoint its best-caching shape. A demoted pair goes back to ordinary `tools` + forced `tool_choice`, so from that point it lands in the per-pass lanes measured above rather than the one shared prefix.

The call that finds it **re-issues itself** in the new shape rather than losing its pass. That retry is clean precisely because a forced call buffers its arguments instead of streaming them: at the moment the schema is disproved, nothing but reasoning deltas has reached the caller. (Same shape as `workflows/_forced_call.py`'s retry after `note_forced_tool_choice_ignored`. Two costs, both bounded to once per model per process: the pass's reasoning box shows two thinking runs, and the KV tracker sees only the surviving attempt's usage, so that call under-reports its true tokens by the discarded one.) A `tools_in_prompt=False` caller is the exception — its prefix must stay schema-free, so re-planning would rebuild the identical request; it demotes for everyone else's benefit and keeps its own degraded reply.

Passes read the policy per call, so the rest of the turn moves lanes along with the pass that found it. That is not a cost worth deferring to a turn boundary: each new lane's cold start is paid on whichever turn the switch lands on, so gating would move the same prefill one turn later while leaving the rest of *that* turn's Director calls forcing a schema nobody applies.

Keeping the endpoint knob and revoking per model is deliberate -- the alternative is a hand-maintained list of which models behind a proxy honor structured output, which nobody can keep accurate.

This has two consequences worth knowing about:

- **Schemas for tools a pass can't use are still sent.** If `direct_scene`, `editor_apply_patch`, and length guard are all on, every pass — including the director, which can only call `direct_scene` — ships schemas for `direct_scene`, `editor_apply_patch`, **and** `editor_rewrite`. Sent, not necessarily rendered: what the server does with them depends on that pass's `tool_choice`, per the table above.
- **Schema order is load-bearing.** The same six schemas in reverse order cost 12.5% of the cached prefix on Ionstream and 28% on GPT-4.1-mini. `enabled_schemas()` iterates `TOOLS` in registry order, and that stability is the reason it caches — it is not incidental dict ordering.
- **Dynamic schemas are built once per turn, not once per pass.** `direct_scene`, `give_feedback`, and `record_direction_note` are all assembled at runtime from the user's enabled interactive fragments, which inject custom string/array properties into each function's parameters. Each schema is built one time per turn from the current fragment set and then threaded through every pass. Their shapes depend only on the fragment configuration, never on per-turn state — so the same fragment set produces the same schema bytes turn after turn.
- **The post-writer feedback step is not a cache exception.** `give_feedback` produces the out-of-character note shown to the player. It rides the shared per-turn tools blob exactly like `direct_scene` (built once from the enabled `feedback`-type fragments, threaded to every pass), so the feedback step reuses the same frozen cached base as the director/writer/editor and merely forces `tool_choice=give_feedback`. It used to swap the tools blob onto a copy of the base, making one deliberate cache miss — that is gone. The step must also extend the stack on the *message* side: it replays the writer's exact user message and the reply as a real `assistant` turn (mirroring the editor), so it continues the warm writer/editor prefix. Appending a single fresh user message after `base.prefix` instead — the original feedback shape — forks the stack and collapses the provider's prefix-cache hit to just the system+tools block, even though the prefix bytes are identical; servers reuse a prefix you *continue from*, not one you fork off. (It also leaves a clean turn continuation for the next turn's director to extend, so a forked feedback call busts the following director too.)
- **The direction-note step is the same shape.** `record_direction_note` persists the Director's running notes (see [Direction Notes](../features/direction-notes.md)); its post-turn placement rides the shared blob and replays the writer exchange exactly like the feedback step, so it is not a cache exception either. The before-writer placement appends only its request to the shared prefix, since there is no written reply to replay yet.

**Text-completion mode dissolves this invariant on its native path.** When an endpoint runs in `text` mode (`completion_mode='text'`, llama.cpp's native `/apply-template` + `/completion`), tool schemas are **never rendered into the prompt** — forced passes constrain output with a `json_schema` grammar instead, and non-forced passes ride the client-side `parse_tool_calls` recovery chain. If `/apply-template` fails, the chat fallback explicitly withholds schemas too, preserving that contract. An image-bearing call is the deliberate exception: native text completion cannot carry images, so the call selects chat transport up front and follows the chat endpoint's normal tool policy. `ModelLane.sends_tool_schemas()` answers from the complete message shape (frozen history plus trailing call), keeping the no-tools writer nudge symmetric with whichever transport will actually run. On the native path the cached prefix is system + history only (strictly *better* caching than chat mode, which must serialise the shared tool blob into the prefix). The shared `CachedBase.tools` blob still exists and is still byte-identical across passes — it just isn't part of the native text-path bytes. Invariants 1/2/4/5 are unaffected; llama.cpp caches by token prefix, so the per-pass reasoning-split fork (§9) doesn't apply. Two mode-specific mechanics live in `backend/inference/text_completion.py`: the reasoning-tag triple is sniffed once per server from `/props` and cached module-level, and provider `usage` is synthesised from the final `/completion` chunk (`cached_tokens = tokens_evaluated − timings.prompt_n`) so the tracker's provider-truth path works unchanged.

**Document mode's Output Auditor follows the same extend-don't-fork rule.** The patch call (`features/documents/audit.py`) byte-extends the generation prompt rather than building its own conversation: text mode appends draft + audit report to the exact prompt string the generation sent (re-running the `/apply-template` render for assisted docs — a closed assistant turn can render *different* bytes than the open generation prompt the draft actually followed, e.g. Qwen's injected `<think></think>` block) and forces JSON via `json_schema` on `/completion`; chat mode replays the generation messages verbatim and forces via `tools_in_prompt=False` → `response_format`, keeping the tool schema out of the server-rendered prompt the generation never had. Regression-tested by `tests/unit/test_document_kv_parity.py`.

### Invariant 4 — Director output rides on the trailing message, never on the system prompt

The director picks moods, plot direction, progressive state, etc. None of that mutates the system prompt or the history. The style injection block is bolted onto the writer's trailing user message, at the top of the stack where cache misses are cheap and bounded to a single pancake. The trailing lorebook block follows the same rule — it carries only the per-turn selections (keyword hits + Director picks); constant entries are the one lorebook piece that lives in the prefix instead (see Invariant 1), precisely because they never change.

### Invariant 5 — When the agent uses a separate model, the writer drops tools

If the director and editor are configured to run on a different model than the writer, the writer's KV cache lives on a different inference server and can't be shared with the agent passes. In that case, the writer sends no tools at all — including them would just waste tokens with no caching benefit. The agent passes still share a cache with each other on their own server.

### How these invariants are enforced in code

Invariants 1–3 and 5 are not left to each pass to honour by convention. The cached bottom — prefix + tools blob + model — is captured once per turn per server in a frozen `CachedBase` (`backend/inference/cached_call.py`), built in `_resolve_pipeline_config`. A single-model turn has one base shared by all three passes; a dual-model turn has two (an agent base for director + editor, and a writer base whose tools blob is empty — that *is* Invariant 5). Passes never call `enabled_schemas` or assemble the prefix themselves; they call `base.complete(trailing=…, tool_choice=…)`, which extends the frozen bottom with only the per-pass top. Because the cache-relevant bytes are computed in exactly one place and the base is immutable, a pass cannot reconstruct them differently and so cannot silently diverge. `base.complete` routes through `cached_complete`, so the KV tracker always records the exact bytes that were sent (see §8). Structured-output endpoints join text mode as the exception: the tracker still records the tools blob even though the wire never carries it (withheld from the chat body on those endpoints -- see Invariant 3; never rendered at all in text mode).

> **What the base does *not* capture: the reasoning mode.** A shared `CachedBase` guarantees identical prefix bytes, but reasoning is toggled per pass outside the base (`reasoning_cfg(on)` in the `complete` call). On backends that route thinking-on and thinking-off down separate KV caches, a per-pass `reasoning_enabled_passes` split forks the cache *underneath* an otherwise-correct single base — the bytes match, but they land in different lanes. This is the one way single-model mode can stop behaving like "one shared prefix." See §9.

---

## 5. Walk-through: one full turn

Let's trace what happens when the user types "I draw my sword." in a turn where all features are on.

### Step 1 — Build the prefix (once)

```python
prefix = [
    {"role": "system",    "content": "<all character/scenario text>"},
    {"role": "user",      "content": "Hi!"},
    {"role": "assistant", "content": "Hello, traveler..."},
    ...
]
```

### Step 2 — Director call

```python
msgs = prefix + [{"role": "user", "content": "[OOC...] Call ONLY direct_scene ...\nUser's next message: \"I draw my sword.\""}]
client.complete(messages=msgs, tools=ALL_SCHEMAS, tool_choice={direct_scene})
```

The model returns: `direct_scene(moods=["tense", "combat"], keywords=["steel", "stance"])`.

### Step 3 — Writer call

```python
inj_block = "**Scene Direction**\n<Mood1 content>: ...\n<Mood2 content>: ...\n"
msgs = prefix + [{"role": "user", "content": "<lorebook>\n<inj_block>\nI draw my sword."}]
client.complete(messages=msgs, tools=ALL_SCHEMAS, tool_choice="none")
```

The entire `prefix` is reused from the director call. Only the trailing user message is new. On a long conversation, that's typically 90%+ of the prompt cached.

The model streams: "Steel rings as the blade leaves its sheath..."

### Step 4 — Editor call (if audit finds issues)

```python
msgs = prefix + [
    {"role": "user",      "content": "<lorebook>\n<inj_block>\nI draw my sword."},  # same as writer's
    {"role": "assistant", "content": "Steel rings as the blade leaves its sheath..."},  # writer's draft
    {"role": "user",      "content": "[OOC: you are the editor...] Apply patches to fix: <numbered audit report>"},
]
client.complete(messages=msgs, tools=ALL_SCHEMAS, tool_choice={editor_apply_patch})
```

`prefix` + the writer's trailing user message are both reused, and the writer's draft was cached as the writer streamed it — so in single-model mode only the editor instructions are genuinely new (in dual-model the draft is fresh on the agent server; see §7). The cached prefix is many thousands of tokens either way.

---

## 6. What happens across turns

When the user sends another message, the new turn's prefix is **the old prefix plus one (user, assistant) pair**:

```
Turn N writer prompt:  [system, m1, m2, ..., m_k, writer_pancake_N]
Turn N+1 director:     [system, m1, m2, ..., m_k, user_N, asst_N, director_pancake_{N+1}]
```

The bottom `[system, m1, ..., m_k]` is byte-identical. The cached portion of turn N's writer call carries over to turn N+1's director call. That's why long sessions don't get linearly slower per turn — most of the prompt is already in the server's KV cache.

Message content upholds this because inline macros (`{{roll}}`/`{{random}}`) fire once at the persist boundary — the row already holds the final text, so `user_N` replayed as history in turn N+1 is byte-identical to the trailing pancake that carried it in turn N.

---

## 7. The editor's ReAct loop

The editor can iterate a few times before producing its final output. Within those iterations the bottom of the stack — system + history + writer's user message + writer's draft — is held constant, and only the top — the editor instruction plus any prior tool-call/tool-result pair — changes each round. The pattern is the same as the cross-pass design: keep the bottom sacred, let the top vary.

How well that bottom is *already* cached when the loop starts depends on whether the agent and writer share a model:

### Single-model mode (writer + agent on the same server)

The editor's iteration-1 bottom is already hot: system + history + the writer's trailing user message + draft were cached when the writer streamed its response. Iteration 1 only pays a cache miss for the editor instruction itself; iterations 2+ pay a miss only for the new tool-call/tool-result turn at the top.

This holds because the editor and writer share a **reasoning mode** (both thinking-off), so they share a cache lane. Either way the pre-warming credit for the writer-pancake-and-draft slice belongs to the writer, not the director — the director's call never contained those bytes. And if the director is switched to thinking-on (a non-default setting; the default ships all three off), it rides a *separate* lane and shares nothing with the writer/editor within the turn at all (see §9).

### Dual-model mode (`agent_same_as_writer = false`)

The editor runs on the agent server, which has the director's cache but **never saw the writer's call**. So iteration 1's bottom is only partially hot: `agent_prefix` (system + history under the agent's system prompt) is cached from the director, but the writer's user message and the writer's draft are novel bytes on this server. Iteration 1 pays a cache miss for that whole writer-pancake-and-draft slice plus the editor instruction. From iteration 2 onward the loop behaves identically to single-model — the bottom is now cached on the agent server, and only the new top pancake is new.

In other words: the **intra-loop** discipline is the same in both modes. What differs is the **cross-pass** hand-off into iteration 1 — in single-model the writer pre-warms the editor's bottom; in dual-model the editor has to warm that slice itself on the agent server, and the saving only kicks in from the second iteration.

---

## 8. The KV tracker

Orb logs cache behaviour for each LLM call in two views:

- **Provider** — ground truth. The `usage` field returned by the model server reports how many prompt tokens it actually served from cache. This is the only number that reconciles with the provider's billing dashboard, and it's what to trust when you want to know whether the cache hit.
- **Local estimate** — a debugging aid alongside provider truth, split into two parts that are deliberately *not* combined:
  - A character-prefix overlap of the messages list (without tools), giving a percentage shared with the predecessor call.
  - A binary match/differ on the tools blob.

The split exists because where a chat template renders the tools list (inside the system block, before the final user turn, or somewhere else) determines whether a tools diff actually breaks the wire-level cache. The tracker can't inspect the template, so collapsing the two signals into one "estimated %" would lie. Two split numbers plus provider truth lets a human read what's going on without false precision.

There is a third thing the tracker cannot see: which schemas the server actually rendered. Orb records the blob it sent, and on most backends that is not what reached the prompt (Invariant 3). The tell is a turn where `msgs_overlap` reads near 100% and the provider `cached` reads near 0 — same signature as the reasoning fork in §9, arriving from tool rendering instead. To tell the two apart, check whether the passes differ in `tool_choice` or in reasoning mode.

The tracker also remembers the previous turn's snapshot per conversation, so the first call of a new turn is compared against the same-label call from the previous turn rather than reported as a baseline.

---

## 9. Caveat: a per-pass reasoning split forks the cache

Everything above assumes that passes sharing a base also share a **reasoning mode**. By default they do: `reasoning_enabled_passes` ships as `{"director": false, "writer": false, "editor": false}` — all three thinking-off, one shared lane, no fork. But the setting is per-pass, so a non-default configuration that turns reasoning on for some passes and not others breaks that assumption. Take the canonical example — director thinking-on, writer and editor off (`{"director": true, "writer": false, "editor": false}`):

On a backend that routes thinking-on and thinking-off down different paths with **separate KV caches** (DeepSeek is the one we've measured), that split configuration splits the single-model cache in two:

- **thinking-ON lane** — the director.
- **thinking-OFF lane** — the writer and the editor.

Both lanes hold byte-identical prefixes (same system + history + tools, from the same `CachedBase`), but they **cannot reuse each other's cache**. So within a turn the writer does *not* inherit the director's freshly-warmed prefix, even though single-model mode put them on the same endpoint. Each pass instead reuses its own same-mode call from the **previous** turn. From a real log:

```
director:direct_scene   cached=3072/5257 tok (58.4%)   ← from the previous turn's director (ON lane)
writer                  cached=2176/4297 tok (50.6%)   ← from the previous turn's writer (OFF lane), NOT this turn's director
```

The tell is the gap between the two tracker views (§8): the local `msgs_overlap` reads ~91% (the prefix bytes *are* shared) while the provider `cached` sits far lower — exactly the "msgs_overlap high, provider lower, template-dependent" case the tracker is built to surface. The counter-intuitive result — the director showing *more* cached than the writer that ran right after it — is not cross-pass reuse at all; it's two independent lineages, each warmed by its own prior-turn call.

**When you opt into it, this is a trade-off, not a bug.** A pass you've set to reason (here the director) does so on purpose, and the cache still pays off **across turns within each lane** — you're just keeping two warm prefixes instead of one. The shipped default sidesteps it entirely by keeping all three passes off (uniform → one lane). To collapse the lanes back after diverging them, make the reasoning mode uniform across the passes again (set all three the same in `reasoning_enabled_passes`), accepting the trade-off: either the director loses its reasoning, or the writer pays for thinking on the main generation. On backends that *don't* fork the cache by thinking mode, the split is free and this whole section is moot.

A stepped, click-through walkthrough of the mechanism and this fork lives in [kv-cache-animation.html](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html).

### Off-turn image prompter

Image generation's `analyze_scene` and `compose_image_prompt` calls run on the
resolved Agent lane: the shared Writer/Agent client in single-model mode, or the
Director/Editor endpoint, model, and agent system prefix in dual-model mode. The
off-turn prefix builder must therefore reproduce the corresponding pipeline
prefix byte-for-byte; parity for both model topologies is regression-tested.

That includes a group, whose prefix is a different document altogether: the
cast section stands in for the card, `{{char}}` is the scene title, `{{cast}}`
is the roster, and every assistant line carries its speaker's name. Both
builders resolve it through the one `resolve_cast` in `database/queries/
group_members.py` — which is *why* it lives in the query layer rather than
beside the pipeline's cast policy, since `workflows/` sits below `pipeline/`
and may not import it. An off-turn call names no speaker, so it reproduces the
neutral base: the same one the Director runs on in all three context modes.

The prompter has its own `prompter_reasoning` switch rather than inheriting a
pipeline pass. Both calls always use the same switch value and the same
order-stable two-tool schema blob, so they stay in one reasoning lane and reuse
one another — *unless* the provider won't honor a forced `tool_choice` (DeepSeek
with thinking on rejects it; OpenRouter and llama.cpp's chat endpoint sometimes
ignore it). Forcing is what makes the shared blob safe: without it the model
picks from the array and answers `analyze_scene` when `compose_image_prompt` was
asked for. So on those providers `forced_tool_call` ships only the forced tool,
and the two calls no longer share a prefix. That costs one extra cache miss per
image, only on the analysis path (without analysis there is just one call) — and
it is a *whole-prefix* miss, not a tail miss: templates render the tool
declarations ahead of the conversation (llama.cpp's Gemma 4 template emits them
inside the first system turn), so a different tools array diverges at the front
and `compose` re-prefills the entire conversation rather than resuming after it.
That position is per-backend, not universal — some render the block near the end,
where the same divergence costs a flat few hundred tokens instead. Invariant 3 has
the measurements; this is the general case, not a prompter quirk.
Shipping one tool rules out the wrong tool; a provider that ignores `tool_choice`
is still free to answer with no tool call at all, which degrades to empty args as
before. The trade buys a composed prompt at all — the previous behavior was a
hard failure, not a cheaper success.

Matching Editor reasoning is a useful cross-pass heuristic because
it is the same Agent server and often the latest Agent-side call, but it is not
an invariant: the Editor may be skipped, providers differ, and the prompter's
standalone tools can create a distinct templated prefix. Keeping the prompter
setting stable is the only portable cache rule.

---

## 10. TL;DR

- Treat the prompt like a stack: bottom is sacred (system + history + tool schemas), top is freely mutable.
- Same tool schemas everywhere, even when a pass can't use them — but don't read that as one shared prefix. Most servers render schemas according to each pass's `tool_choice`, so director, writer and editor land on three different prompts; `"none"` renders nothing at all. Measured numbers in Invariant 3. Dynamic schemas (`direct_scene`, `give_feedback`, `record_direction_note`) are built once per turn from configuration, not per pass. The post-writer feedback and direction-note steps share the base too — neither is a cache exception.
- Director output rides on the trailing user message, not the system prompt.
- The editor extends the writer's stack, not the bare prefix — that's where most editor-pass savings come from.
- Across turns, the new prefix is "old prefix + one (user, assistant) pair," so cache flows naturally turn-over-turn.
- Provider `usage` is the truth; the local tracker is an indicator, deliberately unfused so it doesn't lie.
- The shipped default runs all three passes thinking-off (`reasoning_enabled_passes` all false) — uniform, so one shared lane and no fork. A non-default config that diverges the passes (e.g. director thinking-on, writer + editor off) forks the cache on backends that separate thinking-on/off (DeepSeek): each mode rides its own lane, so passes in different modes don't share *within* a turn — only across turns within each lane. Keep `reasoning_enabled_passes` uniform to avoid the split. See §9.
