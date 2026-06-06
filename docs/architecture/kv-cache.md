# How Orb Reuses the LLM's KV Cache

This doc explains, in plain English, how Orb keeps each LLM call fast and cheap by carefully reusing the **KV cache** — the working memory the model builds up as it reads a prompt.

Audience: someone who can read code but isn't deep in LLM internals. No tokenisation math, no transformer diagrams.

> **Animation:** [kv-cache-animation.html](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html) is a stepped, self-contained walkthrough of the mechanism across all three passes and two turns — and of the reasoning-mode fork that silently splits the cache when `reasoning_enabled_passes` differs across passes (the default: director on, writer/editor off). Open it in a browser.

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

`tool_choice` is forced to `direct_scene` (or `rewrite_user_prompt`). The model returns a tool call, never raw prose.

### Writer pass (1 call)

```
  system + history                ← cached prefix (same as director's)
+ lorebook block
+ "**Scene Direction**\n<Mood content>"   ← injected from director's output
+ user's actual message
```

`tool_choice="none"`. The model writes prose.

### Editor pass (1–3 calls, only if needed)

```
  system + history                ← cached prefix (same as writer's)
+ writer's exact user message     ← reuses the writer's trailing pancake
+ assistant: <writer's draft>     ← the prose the writer just produced
+ "[OOC: you are the editor...] Apply patches to fix: ..."
```

`tool_choice` is forced to `editor_apply_patch` or `editor_rewrite`.

The editor's prompt **extends** the writer's prompt: the writer's trailing user message is reused verbatim. So the editor's cached prefix isn't just system + history; it's all of that **plus the writer's pancake plus the writer's draft**. That's the bulk of where the editor's savings come from.

---

## 4. The invariants that keep the cache intact

### Invariant 1 — One system prompt, shared by every pass

Character card, persona, scenario, example dialogue, post-history instructions, and user description are concatenated into a single system message once per turn. The same string is sent to all three passes. No pass adds, edits, or reorders anything.

### Invariant 2 — One history list, shared by every pass

The chat history is built once per turn. Each pass receives the same list. Attachments (images) are encoded with the same bytes on every reference.

### Invariant 3 — One tool list, shared by every pass

Inference servers serialise the tool schema list into the cached prefix (where in the chat template depends on the server, but it's always *inside* the cached region). So the tool list has to be byte-identical across passes — including passes that won't call any tool. Every pass sends the same schemas; passes that aren't allowed to call them set `tool_choice="none"`.

This has two consequences worth knowing about:

- **Schemas for tools a pass can't use are still sent.** If `direct_scene`, `editor_apply_patch`, and length guard are all on, every pass — including the director, which can only call `direct_scene` — ships schemas for `direct_scene`, `editor_apply_patch`, **and** `editor_rewrite`.
- **Dynamic schemas are built once per turn, not once per pass.** `direct_scene` and `give_feedback` are both assembled at runtime from the user's enabled interactive fragments, which inject custom string/array properties into each function's parameters. Each schema is built one time per turn from the current fragment set and then threaded through every pass. Their shapes depend only on the fragment configuration, never on per-turn state — so the same fragment set produces the same schema bytes turn after turn.
- **The post-writer feedback step is not a cache exception.** `give_feedback` produces the out-of-character note shown to the player. It rides the shared per-turn tools blob exactly like `direct_scene` (built once from the enabled `feedback`-type fragments, threaded to every pass), so the feedback step reuses the same frozen cached base as the director/writer/editor and merely forces `tool_choice=give_feedback`. It used to swap the tools blob onto a copy of the base, making one deliberate cache miss — that is gone. The step must also extend the stack on the *message* side: it replays the writer's exact user message and the reply as a real `assistant` turn (mirroring the editor), so it continues the warm writer/editor prefix. Appending a single fresh user message after `base.prefix` instead — the original feedback shape — forks the stack and collapses the provider's prefix-cache hit to just the system+tools block, even though the prefix bytes are identical; servers reuse a prefix you *continue from*, not one you fork off. (It also leaves a clean turn continuation for the next turn's director to extend, so a forked feedback call busts the following director too.)

### Invariant 4 — Director output rides on the trailing message, never on the system prompt

The director picks moods, plot direction, progressive state, etc. None of that mutates the system prompt or the history. The style injection block is bolted onto the writer's trailing user message, at the top of the stack where cache misses are cheap and bounded to a single pancake.

### Invariant 5 — When the agent uses a separate model, the writer drops tools

If the director and editor are configured to run on a different model than the writer, the writer's KV cache lives on a different inference server and can't be shared with the agent passes. In that case, the writer sends no tools at all — including them would just waste tokens with no caching benefit. The agent passes still share a cache with each other on their own server.

### How these invariants are enforced in code

Invariants 1–3 and 5 are not left to each pass to honour by convention. The cached bottom — prefix + tools blob + model — is captured once per turn per server in a frozen `CachedBase` (`backend/kv_tracker.py`), built in `_resolve_pipeline_config`. A single-model turn has one base shared by all three passes; a dual-model turn has two (an agent base for director + editor, and a writer base whose tools blob is empty — that *is* Invariant 5). Passes never call `enabled_schemas` or assemble the prefix themselves; they call `base.complete(trailing=…, tool_choice=…)`, which extends the frozen bottom with only the per-pass top. Because the cache-relevant bytes are computed in exactly one place and the base is immutable, a pass cannot reconstruct them differently and so cannot silently diverge. `base.complete` routes through `cached_complete`, so the KV tracker always records the exact bytes that were sent (see §8).

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
    {"role": "user",      "content": "[OOC: you are the editor...] Apply patches to fix: <audit report>"},
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

---

## 7. The editor's ReAct loop

The editor can iterate a few times before producing its final output. Within those iterations the bottom of the stack — system + history + writer's user message + writer's draft — is held constant, and only the top — the editor instruction plus any prior tool-call/tool-result pair — changes each round. The pattern is the same as the cross-pass design: keep the bottom sacred, let the top vary.

How well that bottom is *already* cached when the loop starts depends on whether the agent and writer share a model:

### Single-model mode (writer + agent on the same server)

The editor's iteration-1 bottom is already hot: system + history + the writer's trailing user message + draft were cached when the writer streamed its response. Iteration 1 only pays a cache miss for the editor instruction itself; iterations 2+ pay a miss only for the new tool-call/tool-result turn at the top.

This holds because the editor and writer default to the **same reasoning mode** (both thinking-off), so they share a cache lane. The director does *not* contribute to this warmth: with the default `reasoning_enabled_passes` it runs thinking-on, in a separate lane (see §9). So the pre-warming credit here belongs to the writer, not the director.

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

The tracker also remembers the previous turn's snapshot per conversation, so the first call of a new turn is compared against the same-label call from the previous turn rather than reported as a baseline.

---

## 9. Caveat: a per-pass reasoning split forks the cache

Everything above assumes that passes sharing a base also share a **reasoning mode**. By default they don't. `reasoning_enabled_passes` ships as `{"director": true, "writer": false, "editor": false}` — the director thinks, the writer and editor don't.

On a backend that routes thinking-on and thinking-off down different paths with **separate KV caches** (DeepSeek is the one we've measured), that single setting splits the single-model cache in two:

- **thinking-ON lane** — the director.
- **thinking-OFF lane** — the writer and the editor.

Both lanes hold byte-identical prefixes (same system + history + tools, from the same `CachedBase`), but they **cannot reuse each other's cache**. So within a turn the writer does *not* inherit the director's freshly-warmed prefix, even though single-model mode put them on the same endpoint. Each pass instead reuses its own same-mode call from the **previous** turn. From a real log:

```
director:direct_scene   cached=3072/5257 tok (58.4%)   ← from the previous turn's director (ON lane)
writer                  cached=2176/4297 tok (50.6%)   ← from the previous turn's writer (OFF lane), NOT this turn's director
```

The tell is the gap between the two tracker views (§8): the local `msgs_overlap` reads ~91% (the prefix bytes *are* shared) while the provider `cached` sits far lower — exactly the "msgs_overlap high, provider lower, template-dependent" case the tracker is built to surface. The counter-intuitive result — the director showing *more* cached than the writer that ran right after it — is not cross-pass reuse at all; it's two independent lineages, each warmed by its own prior-turn call.

**This is intentional, not a bug.** The director reasons on purpose, and the cache still pays off **across turns within each lane** — you're just keeping two warm prefixes instead of one. To collapse the lanes back into a single shared cache, make the reasoning mode uniform across the passes (set all three the same in `reasoning_enabled_passes`), accepting the trade-off: either the director loses its reasoning, or the writer pays for thinking on the main generation. On backends that *don't* fork the cache by thinking mode, the split is free and this whole section is moot.

A stepped, click-through walkthrough of the mechanism and this fork lives in [kv-cache-animation.html](https://orbfrontend.github.io/Orb/architecture/kv-cache-animation.html).

---

## 10. TL;DR

- Treat the prompt like a stack: bottom is sacred (system + history + tool schemas), top is freely mutable.
- Same tool schemas everywhere, even when a pass can't use them. Dynamic schemas (`direct_scene`, `give_feedback`) are built once per turn from configuration, not per pass. The post-writer feedback step shares the base too — it is no longer a cache exception.
- Director output rides on the trailing user message, not the system prompt.
- The editor extends the writer's stack, not the bare prefix — that's where most editor-pass savings come from.
- Across turns, the new prefix is "old prefix + one (user, assistant) pair," so cache flows naturally turn-over-turn.
- Provider `usage` is the truth; the local tracker is an indicator, deliberately unfused so it doesn't lie.
- A per-pass reasoning split forks the cache on backends that separate thinking-on/off (DeepSeek): the director (thinking on) rides one lane, the writer + editor (thinking off) another, so they don't share *within* a turn — only across turns within each lane. Make `reasoning_enabled_passes` uniform to collapse them. See §9.
