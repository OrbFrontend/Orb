# Document Mode — Assisted continuation via inline role macros

## Goal

Let a user drive an **instruct** model from document mode **without hand-typing
chat-template control tokens**. Instead of `<|im_start|>…`, the user writes
readable line macros and we render them through llama.cpp `/apply-template`
so the model's own template (BOS, turn markers, everything) is applied for them.
This is an **opt-in prompting strategy**, orthogonal to the text/chat transport —
"Raw" (current, mikupad-style) stays the default.

The document is a **transcript**: notes and prose interleave in document order
and round-trip faithfully — each generation extends the document, and re-parsing
the extended document reproduces the same conversation plus the new text.

## The convention

Line-anchored macros. A line whose start matches
`^###\s*(SYSTEM|USER|ASSISTANT)\s*:\s?(.*)$` (case-insensitive) is a **role
header**; its inline remainder is that role's content. **Every non-macro line is
continuation (assistant) prose.** Newlines are preserved verbatim inside a
prose block.

```
### SYSTEM: You are a co-writer. Continue in the same voice, tense, and style.
### USER: Write a story about a monkey. Write flowery, ornate prose.
Once upon a time, beneath the gilded canopy of a jungle resplendent with
morning light, there lived a monkey of singular disposition…
### USER: Write tersely from now on. Short sentences.
The monkey woke. He▮
```

- `### SYSTEM:` → system turn (pre-filled with `DOC_ASSIST_INSTRUCTION`,
  editable; delete the line to fall back to the default). All SYSTEM lines join
  into **one** system turn hoisted to the front, wherever they appear.
- `### USER:` → a **user turn at its document position**. Consecutive USER
  lines join into one turn (that's how you write a multi-line note).
- Prose between/around notes → **assistant turns in document order**. The
  **final** prose block is streamed as an open **prefill** so the model
  continues it from the cursor `▮`; every earlier prose block is a **closed**
  assistant turn.
- `### ASSISTANT:` → its inline content joins the surrounding prose block
  (prose already *is* the assistant turn; matching the macro keeps the line
  from leaking literally into the prompt, and serves as an escape hatch for
  prose that would otherwise parse as a macro).

The reading this gives the model is causal and matches template semantics:
**a note governs the prose below it**, and prose above a note was written
under the note above *that*. Two placements fall out:

- **Note above existing prose** → that prose is its (possibly still-open)
  response; generation continues the prose under the note's instruction.
- **Note at the bottom** (no prose after it) → empty prefill; the model opens
  a **fresh turn** responding to the note — a new paragraph, not a mid-sentence
  continuation. To steer an in-progress sentence, the note goes *above* it.

Macros are **only** interpreted when Assisted is on. In Raw mode `### SYSTEM:`
is literal prose sent verbatim — no behavior change for existing docs.

### Parsing

A single line scan, no message tree: classify each line, coalesce runs into
blocks (SYSTEM-run / USER-run / prose-run), then emit blocks in order as
alternating messages.

```python
def parse_doc_macros(text: str) -> tuple[list[ChatMessage], str | None]:
    # returns (messages, prefill):
    #   messages — [system, user, assistant, user, …]; always starts
    #              [system, user] and alternates by construction
    #   prefill  — final prose block verbatim, or None if the doc ends
    #              with a note (fresh-turn generation)
```

- No `### SYSTEM:` → system defaults to `DOC_ASSIST_INSTRUCTION`.
- No `### USER:` anywhere → single default user turn
  `"Continue the text. Write several paragraphs."` and the whole doc is the
  prefill — byte-identical to the previously validated 3-turn shape, so
  macro-free assisted docs behave exactly as before. The length hint is
  load-bearing (probe-verified on Qwen): bare "Continue the text." → 6–9 tok
  before EOS; with the hint → 50–300 tok.
- Doc **starts with prose** but has later notes → the same default user turn is
  inserted **before** the leading prose as alternation filler. Gemma-4's
  template accepts a leading assistant turn (probe-verified), but stricter
  templates (Mistral-style alternation asserts) raise, and an
  `/apply-template` failure degrades badly (see edge cases) — the filler is
  cheap insurance. When real notes exist, the *last* note is the operative
  instruction; a user who wants long output puts the length hint in their note.
- Alternation is **structural**: prose accumulates until a USER line and vice
  versa, so same-role adjacency can't occur. Whitespace-only prose between two
  notes is dropped (the notes join into one turn). Closed assistant turns are
  right-trimmed (cosmetic inside a closed turn); the prefill is kept verbatim —
  a whitespace-only prefill becomes `None`.
- Empty macro content → macro ignored.

## Server validation

Probed the real endpoint before designing. Single-turn results below from
`http://localhost:5000` (both models); **multi-turn probes 2026-07-09** against
`http://100.95.103.73:5000` — first Gemma-4-31B-it, then Qwen3.6-27B after a
reload the same day, so **every multi-turn claim below is verified on both
models**. Re-audited 2026-07-08 against Qwen3.6-27B: all single-turn Qwen
claims reproduced.

Common to both models: **`/apply-template` leaves the trailing assistant turn
open for prefill** (ends with the prefill, no closing turn token), so the model
continues from it; **`chat_template_kwargs.enable_thinking` has no effect under
prefill** (open-turn rendering bypasses the generation-prompt branch — verified
both directions on Qwen); **the server prepends BOS** per the model's own
config, so the user never types it.

**Reasoning is always off in assisted mode** — pass `reasoning_cfg(False)` on
every assisted call, both transports. On the text path it's a **no-op** (under
prefill `_complete_text` skips `chat_template_kwargs` entirely, `client.py:448`,
and `build_completion_params` drops reasoning keys by allowlist); the **chat
fallback is what actually honors it**. The empty `<think>` block Qwen injects
before the prefill means the template itself also forces non-thinking there.

### Multi-turn rendering & obedience (both models, 2026-07-09)

- `[system, user, assistant, user, assistant]` renders with **every turn closed
  except the last**, which ends at the prefill — the open-prefill property
  holds unchanged in multi-turn. ✅ both models. **Closed assistant turns carry
  no thinking scaffold**; only the open turn gets it (Gemma:
  `<|channel>thought\n<channel|>`, Qwen: `<think>\n\n</think>\n\n` before the
  prefill).
- **Leading assistant** (prose before any note) and **trailing user** (note at
  bottom) both render without error on both templates; trailing user yields the
  generation prompt — the empty-prefill fresh-turn case. ✅ **Qwen footgun
  caught here**: with no kwargs its generation prompt ends in an *open*
  `<think>` (thinking defaults on), i.e. the model would think straight into
  the document. With `chat_template_kwargs: {enable_thinking: false}` it
  renders the closed empty think block instead (probe-verified) — and that is
  exactly what `_complete_text` sends on the no-prefill path, so the standing
  "always pass `reasoning_cfg(False)`" rule is **load-bearing** for the
  trailing-note case, not just hygiene.
- **Style switch obeyed on both**: turn 1 note "flowery, ornate prose" (and
  matching prose), turn 2 note "Write tersely. Short sentences.", prefill
  continuing mid-sentence → Gemma: `" leaped. He stole a fruit. He ate it. He
  laughed."` (15 tok, `stop: eos`); Qwen (3 seeds): terse staccato
  continuations, seamless from the prefill, **51–91 tok** before EOS. ✅
  (Qwen giving 51–91 rather than its one-sentence 4–25 doesn't contradict the
  single-turn finding — a concrete stylistic note draws a fuller turn than a
  neutral "continue" — but EOS still governs; the design consequence below is
  unchanged.)
- **Honesty note on the contrast probe**: collapsing the same doc to a single
  turn (both notes joined, all prose joined — the old v1 shape) *also* obeyed
  "tersely from now on" on Gemma across 3 seeds. The multi-turn win on this
  probe is not raw obedience; it is **correct causal attribution** (which prose
  was written under which note), template-native turn semantics, and the
  transcript round-trip property. Obedience gaps should widen with more
  turns/contradictory notes, but that is expectation, not probe data.

### Cache behavior across the note boundary (both models, 2026-07-09)

Templates render an **open** assistant turn differently from a **closed** one
(the thinking-scaffold injection above), so when a new note is added below
existing prose, that prose flips open→closed and the rendered prompt
**diverges at the start of the last prose block**, not at its end. What that
divergence costs was measured on both models, and the result corrects an
earlier draft of this plan:

- **There is no divergent-prefix cache reuse on this server, on either model.**
  Adding a note costs a **full prompt re-eval** despite a long shared prefix:
  Gemma 122/122 tok (one-word mid-prompt mutation: 131/131); Qwen with
  never-seen nonce prose **156/156 tok against a 62-token common prefix**. An
  earlier draft attributed Gemma's behavior to its SWA layers and predicted
  suffix-only re-eval on Qwen — **falsified**: the behavior is identical on
  both loaded models. Whatever the cause (attention architecture or server
  build policy), design for it: reuse happens only on **exact extension** of a
  cached state or **exact restore** of a previously-seen one.
- **Pure "keep going"** (re-generate, no new note) is append-only → cheap on
  both: identical re-send **4–5 tok**, extended prefill **14 tok** (matches the
  earlier Qwen probe's 7). The KV cache stays warm.
- **Previously-seen states restore near-free.** The server runs llama-server's
  host-memory prompt cache (`--cache-ram`-style checkpoints): re-sending an
  earlier prompt after the slot had moved on cost **4 tok** — the state was
  restored from a RAM checkpoint, not recomputed. So undo-and-regenerate and
  revisiting an older document state are also cheap, not just linear appends.
- Perspective: the note-add full re-eval is the same cost class as *any*
  mid-document edit in Raw mode on this server, bounded by document size. Not
  a multi-turn design flaw; document it and move on.

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
  stretches it 5–10× (see the parser defaults above); a length hint in
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
which can be one sentence (Gemma's terse-note probe stopped at 15 tok) or
several paragraphs, and almost never coincides with `n_predict`. `n_predict` is
only a ceiling. Raw mode has the same EOS stop but no turn framing pushing
toward "wrap up the answer," so it tends to run longer.

Treat early EOS as **normal termination, not an error** — the existing finalize
path already handles a short (or empty) generated span. Implications:

- **Recommend Raw for long-form**; **Assisted for steered/short** (obey an author
  note, controlled output).
- **"Keep going" = hit Generate again.** `cache_prompt: true` keeps the KV warm
  for append-only re-generates (probe-verified both models; see cache section),
  so the per-note rhythm is: add a note, generate, generate again until
  satisfied, add the next note. Adding the note is the only step that pays a
  re-eval (a full prompt pass on both loaded models; see the cache section).
- **Do not default `ignore_eos`** (silent off-topic drift; see the Qwen probe
  above). A client-side "auto-continue until N tokens" loop is the safe
  lengthener if users ask — out of scope for v1.

## Backend

Reuses existing machinery — **no new inference plumbing**. `complete(..., prefill=…)`
already renders a trailing assistant message as an open turn on the text transport
([`_complete_text`](../backend/inference/client.py), prefill/F9 path) and drops
prefill on the chat transport (`client.py:136`).

### `features/documents/continuation.py`

- New pure function `parse_doc_macros` (unit-testable, no client) — signature
  and rules in the Parsing section above.
- New constant `DOC_ASSIST_INSTRUCTION` (the default `### SYSTEM:` fill; distinct
  from `DOC_CHAT_INSTRUCTION`, which stays for the chat-endpoint fallback).
- `DocumentContinuer.stream(prompt, model, assisted=False)` gains a branch:

  | transport | assisted | call |
  |---|---|---|
  | text | False | `complete_raw(prompt)` — current Raw path |
  | text | True  | `messages, prefill = parse_doc_macros(prompt)` → `complete(messages, prefill=prefill, **self.params, **reasoning_cfg(False))` — `prefill=None` (doc ends with a note) falls through to the client's generation-prompt branch untouched; `self.params` carries the `max_tokens=512` guard |
  | chat | False | current fallback (`DOC_CHAT_INSTRUCTION` + doc as user turn) |
  | chat | True  | chat transport drops prefill, so close it instead: `parse_doc_macros`, then if prefill is non-empty append it as a **closed** assistant turn plus a user turn `"Continue the text exactly from where it stops — no preamble."` (respond-style is the only reliable option here; quality caveat unchanged) |

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
  ChatML note for Raw. Call out the placement semantics and footguns: a note
  **above prose** steers that prose's continuation; a note at the **bottom**
  starts a *fresh paragraph* (empty prefill — it will not continue the previous
  sentence); a wrapped **multi-line note** needs `### USER:` repeated per line
  (consecutive lines join into one turn).
- Macro lines are **not** specially styled/highlighted (styling inline lines in
  contenteditable means span-wrapping, which breaks the pure-prose invariant).
  They render as normal prose. `chars/4` token counter unaffected.

## Edge cases / v1 simplifications

- **No `### SYSTEM:`** → default `DOC_ASSIST_INSTRUCTION`. **No `### USER:`** →
  default user turn + whole doc as prefill (the validated 3-turn shape;
  see Parsing).
- **Doc starts with prose, notes later** → default user turn inserted as
  alternation filler (see Parsing).
- **Doc ends with a note** → `prefill=None`, fresh-turn generation. Without
  prefill `_complete_text` sends `chat_template_kwargs`
  (`enable_thinking: false` via `reasoning_cfg(False)`) — **required, not just
  correct**: probe-verified on Qwen that without it the generation prompt ends
  in an open `<think>` and the model would think into the document. Templates
  ignore kwargs they don't know (Gemma unaffected).
- **No prose at all** (macros only) → empty prefill; model opens the turn
  fresh. Acceptable.
- **`/apply-template` HTTP failure** (rare): `_complete_text` falls back to the
  chat transport with the prefill already popped, so the fallback request loses
  the final prose block — output may be unrelated to the doc. Accepted for v1:
  llama.cpp's endpoint is solid and the client logs a warning; catching it in
  the slice isn't worth the plumbing. (The alternation filler above keeps
  strict templates from triggering this path in the first place.)
- The parser stays a flat line scan (~30 lines) — no regex engine ceremony, no
  message tree, no turn IDs. The document *is* the state.

## Tests

- **`tests/unit/test_document_continuation.py`**: `parse_doc_macros` cases —
  interleaved notes/prose → alternating messages in document order with final
  prose as prefill; defaults on missing system/user; macro-free doc →
  `[system, default-user]` + whole-doc prefill (backward-compat shape);
  leading prose → filler user turn; trailing note → `prefill=None`;
  consecutive `### USER:` lines join; whitespace-only prose block dropped;
  `### ASSISTANT:` content joins surrounding prose; alternation invariant
  holds on adversarial interleavings. Assisted text path → `complete` called
  with parsed `messages`, `prefill=tail`, `reasoning_cfg(False)` spread.
- **`tests/integration/test_documents.py`**: generate with `assisted:true` in text
  mode asserts the mock `complete` (writer queue) saw the parsed multi-turn
  shape; `assisted` defaults false → still hits `complete_raw`.

## Out of scope for v1

Per-macro visual styling; turn-level operations (regenerate *this* turn,
collapse/summarize old turns — the document is the only state, edit it);
auto-inserting a `### SYSTEM:` scaffold line when the toggle flips on (help
text covers discovery); auto-detecting whether the endpoint model has a
template to enable/disable the toggle; sampler/max-token macros; client-side
auto-continue loops; mitigating the full re-eval on note-add (it equals
Raw-mode edit cost; nothing to do client-side). Chat-endpoint assisted quality
stays model-dependent — text mode is the recommended path (unchanged from the
base plan).
