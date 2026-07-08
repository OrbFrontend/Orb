# Document Mode — Assisted continuation via inline role macros

## Goal

Let a user drive an **instruct** model from document mode **without hand-typing
chat-template control tokens**. Instead of `<|im_start|>…`, the user writes
readable line macros and we render them through llama.cpp `/apply-template`
so the model's own template (BOS, turn markers, everything) is applied for them.
This is an **opt-in prompting strategy**, orthogonal to the text/chat transport —
"Raw" (current, mikupad-style) stays the default.

## The convention

Line-anchored macros. A line whose start matches
`^###\s*(SYSTEM|USER|ASSISTANT)\s*:\s?(.*)$` (case-insensitive) is a **role
header**; its inline remainder is that role's content. **Every non-macro line is
continuation (assistant) prose.** Newlines are preserved verbatim.

```
### SYSTEM: You are a co-writer. Continue in the same voice, tense, and style.
### USER: Author note: write tersely from now on, make sure to mention bananas.
Once upon a time, in a small village, there lived a monkey who▮
```

- `### SYSTEM:` → system turn (pre-filled with `DOC_ASSIST_INSTRUCTION`, editable; delete the line to fall back to the default).
- `### USER:` → user turn — the author's note / steering instruction.
- `### ASSISTANT:` → its inline content joins the prefill in document order
  (prose already *is* the assistant turn; matching it keeps the line from
  leaking literally into the prompt, but it adds nothing prose doesn't).
- The prose (everything not a macro header) → the **assistant turn**, streamed in as a **prefill** and left open so the model continues it from the cursor `▮`.

Parsing collapses to a single `(system, user, prefill)` triple: all `SYSTEM`
inline content joins into one system turn, all `USER` into one user turn, all
prose joins in document order into the assistant prefill. Interleaving position
is **not** preserved (a user note after some prose still becomes *the* user turn) —
document mode has one author note and one story, not a multi-turn chat. Multi-turn
conversations are out of scope (see below).

Macros are **only** interpreted when Assisted is on. In Raw mode `### SYSTEM:`
is literal prose sent verbatim — no behavior change for existing docs.

## Server validation (`http://localhost:5000`, both models tested)

Probed the real endpoint before designing. Both loaded models tested; behavior
**diverges** on continuation length — a load-bearing caveat. Re-audited
2026-07-08 against Qwen3.6-27B: all Qwen claims reproduced (numbers below
updated); Gemma was not loaded then, so its results stand from the original probe.

Common to both: **`/apply-template` leaves the assistant turn open for prefill**
(ends with the prefill, no closing turn token), so the model continues from it;
**`chat_template_kwargs.enable_thinking` has no effect under prefill** (open-turn
rendering bypasses the generation-prompt branch — verified both directions on
Qwen: `true` without a trailing assistant renders an open `<think>\n`, with one
it renders identically to `false`); **the server prepends BOS** per
the model's own config (Qwen has none → prompt starts `<|im_start|>system`), so
the user never types it.

**Reasoning is always off in assisted mode** — pass `reasoning_cfg(False)` on
every assisted call, both transports. On the text path it's a **no-op** (under
prefill `_complete_text` skips `chat_template_kwargs` entirely, `client.py:448`,
and `build_completion_params` drops reasoning keys by allowlist); the **chat
fallback is what actually honors it**. The empty `<think>` block Qwen injects
before the prefill means the template itself also forces non-thinking there.

### Gemma-4-31B-it — assisted works well

- Reasoning template (`<|channel>thought`); prefill lands *inside* the thought
  channel, but end-to-end **continues cleanly, no restart**: `"…a monkey who"` →
  `" thought he was a king… One day, he found a bunch of bananas…"` (~80 tok).
  Matched voice, obeyed the note. ✅

### Qwen3.6-27B — assisted continues seamlessly **but stops after one sentence** ⚠️

- ChatML template injects an empty `<think>\n\n</think>\n\n` block before the
  prefill. Continuation is seamless (`"…a monkey who"` → `" loved bananas."`) —
  **then emits EOS and stops: 4–25 tokens across seeds, `stop: eos`**, with
  `n_predict=150`. A neutral note ("Continue the text.") is no better — 6–9
  tokens. Qwen closes its assistant turn at the first sentence boundary; that's
  inherent to instruct/ChatML turn semantics. A **length hint in the user turn**
  stretches it 5–10× (see the default-instruction note below); a length hint in
  the *system* turn does not (probe-verified: 5–49 tok).
- **Contrast on the same seeds:** Raw (no template) ran to the **150-token
  limit** (`stop: limit`) with coherent long-form story. So on Qwen, *Raw is the
  long-form path and Assisted is effectively sentence-at-a-time.*
- `ignore_eos: true` restores length but is **rejected — and not for
  control-token leakage**: llama.cpp implements `ignore_eos` as a −inf logit
  bias on the EOG tokens, so `<|im_end|>` *cannot* be sampled into the text
  (400-token probe: zero occurrences of `<|im_end|>`/`<|im_start|>`). The real
  failure is worse because it's silent: denied its turn end, the model treats
  `\n\n` as a document separator and **drifts into unrelated fresh vignettes**
  (probe produced three back-to-back off-topic mini-stories), polluting the
  document with prose that looks fine and isn't.

### Design consequence: assisted output ends at the model's turn, not our limit

The important property is **general, not model-specific**: in assisted mode the
model owns its turn and emits EOS whenever *it* judges the response complete —
which can be one sentence or several paragraphs, and almost never coincides with
`n_predict`. `n_predict` is only a ceiling. Qwen making that vivid (one sentence)
and Gemma running long in one test are two points on the same spectrum; the same
prefill on either could stop early. Raw mode has the same EOS stop but no turn
framing pushing toward "wrap up the answer," so it tends to run longer.

Treat early EOS as **normal termination, not an error** — the existing finalize
path already handles a short (or empty) generated span. Implications:

- **Recommend Raw for long-form**; **Assisted for steered/short** (obey an author
  note, controlled output).
- **"Keep going" = hit Generate again.** `cache_prompt: true` keeps the KV warm,
  so re-generating from the extended prefill is cheap and seamless — this is the
  intended interaction for assisted mode, not a workaround. Probe-verified:
  three successive generates stayed seamless and note-obedient; the second
  re-evaluated only **7 tokens** (62 cached).
- **Do not default `ignore_eos`** (silent off-topic drift; see the Qwen probe
  above). A client-side "auto-continue until N tokens" loop is the safe
  lengthener if users ask — out of scope for v1.

## Backend

Reuses existing machinery — **no new inference plumbing**. `complete(..., prefill=…)`
already renders a trailing assistant message as an open turn on the text transport
([`_complete_text`](../backend/inference/client.py), prefill/F9 path) and drops
prefill on the chat transport (`client.py:136`).

### `features/documents/continuation.py`

- New pure function (unit-testable, no client):
  ```python
  def parse_doc_macros(text: str) -> tuple[str, str, str]:
      # returns (system, user, prefill); system falls back to DOC_ASSIST_INSTRUCTION,
      # user falls back to "Continue the text. Write several paragraphs." so the
      # [system,user,assistant] shape we validated always holds. The length hint
      # is load-bearing (probe-verified on Qwen): bare "Continue the text." → 6–9
      # tok before EOS; with the hint → 50–300 tok. Free 5–10× more per click.
  ```
- New constant `DOC_ASSIST_INSTRUCTION` (the default `### SYSTEM:` fill; distinct
  from `DOC_CHAT_INSTRUCTION`, which stays for the chat-endpoint fallback).
- `DocumentContinuer.stream(prompt, model, assisted=False)` gains a branch:

  | transport | assisted | call |
  |---|---|---|
  | text | False | `complete_raw(prompt)` — current Raw path |
  | text | True  | `parse_doc_macros(prompt)` → `complete([system,user], prefill=prose, **self.params, **reasoning_cfg(False))` — `self.params` carries the `max_tokens=512` guard |
  | chat | any   | current fallback; if assisted, split system/note via the parser and fold prose into the user turn (chat transport drops prefill, so respond-style is the only reliable option — quality caveat unchanged) |

### `api/`

- `DocumentGenerateRequest` gains `assisted: bool = False`; route passes it to
  `continuer.stream(...)`. No other route change — SSE/lock/abort all identical.

## Frontend

Generation flow, spans, undo, 5s flush — **all unchanged**; only the request body
gains a flag.

- **Toggle** in `#doc-footer`: `Raw ⇄ Assisted`, persisted in `localStorage`
  (same pattern as `documentMode`). Sent as `assisted` in the generate POST.
- **Help block** ([`index.html` `#doc-help`](../frontend/index.html)) updated:
  document the `### SYSTEM:` / `### USER:` convention for Assisted, keep the raw
  ChatML note for Raw. Call out the two line-anchoring footguns: a wrapped
  **multi-line note** needs `### USER:` repeated per line (contents join); a
  note at the **bottom** of the doc leaves the prefill ending in `\n`, so the
  continuation starts a new paragraph — the prefill ends where the *prose* ends,
  not where the cursor is.
- Macro lines are **not** specially styled/highlighted (styling inline lines in
  contenteditable means span-wrapping, which breaks the pure-prose invariant).
  They render as normal prose. `chars/4` token counter unaffected.

## Edge cases / v1 simplifications

- **No `### SYSTEM:`** → default `DOC_ASSIST_INSTRUCTION`. **No `### USER:`** →
  `"Continue the text. Write several paragraphs."` (keeps the validated 3-turn
  shape; length hint is load-bearing, see parser note).
- **Empty macro content** → macro ignored.
- **No prose** (macros only, cursor right after a header) → empty prefill; model
  opens the turn fresh. Acceptable.
- **`/apply-template` HTTP failure** (rare): `_complete_text` falls back to the
  chat transport with the prefill already popped, so the fallback request is
  `[system, note]` **without the document prose** — output may be unrelated to
  the doc. Accepted for v1: llama.cpp's endpoint is solid and the client logs a
  warning; catching it in the slice isn't worth the plumbing.
- **`ponytail:`** the parser is ~15 lines of line-splitting — no regex engine
  ceremony, no message-tree. Upgrade to true multi-turn only if users ask.

## Tests

- **`tests/unit/test_document_continuation.py`**: `parse_doc_macros` cases
  (macros → triple; defaults on missing system/user; prose-only; interleaved
  collapses to one user + one prose; `### ASSISTANT:` content joins the prefill). Assisted text path → `complete` called with
  `prefill=prose`, `messages==[system,user]`, `reasoning_cfg(False)` spread.
- **`tests/integration/test_documents.py`**: generate with `assisted:true` in text
  mode asserts the mock `complete` (writer queue) saw the parsed shape; `assisted`
  defaults false → still hits `complete_raw`.

## Out of scope for v1

Multi-turn conversations (>1 user/assistant exchange); per-macro visual styling;
`### ASSISTANT:` as an explicit mid-document *turn* (the macro is parsed but its
content just joins the prefill — see convention); auto-inserting a `### SYSTEM:`
scaffold line when the toggle flips on (help text covers discovery);
auto-detecting whether the endpoint model has a template to enable/disable
the toggle; sampler/max-token macros. Chat-endpoint assisted quality stays
model-dependent — text mode is the recommended path (unchanged from the base plan).
