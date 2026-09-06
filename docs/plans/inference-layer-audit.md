# `backend/inference/` — layer audit and relocation plan

Status: proposal, nothing implemented.
Audit date: 2026-09-06. Base commit: `91f7d64`.
Scope: `backend/inference/` (7,683 LOC across 25 modules).

This document is a hand-off for independent review. It states a principle, applies
it, and lists what was checked and cleared so a reviewer does not repeat the work.
Every claim carries a `file:line` anchor. Section 8 records claims made during the
audit that were **withdrawn** on verification — read it to calibrate the rest.

---

## 1. Why this audit exists

The prose rewriter used to live entirely in `inference/`. It was moved out: the
shared llama.cpp server layer stayed (`inference/local_models/llama_server/`) and
the feature became `features/prose_rewriter/`. That move was correct, and it was
made for a reason that generalises — a feature does not belong in `inference/`
even if `inference/` is where it happened to be written.

The question this audit answers: **what else is in there for the same reason?**

The answer is three more tenants of the same kind, one weaker candidate, and one
root cause in the architecture documentation that licensed all of them.

---

## 2. The organizing principle

The existing rule in `AGENTS.md` is a *dependency* rule — it constrains which
layers may import which. `inference/` passes it cleanly (verified, §7). The rule
is silent on the question that actually matters here, which is placement, not
direction.

Proposed test, by **axis of change**:

> A module belongs in `inference/` if its reason to change is **a model, a
> provider, or the cache contract**. It does not belong if its reason to change
> is **a product decision**.

Applied:

| Reason to change | Modules | Verdict |
|---|---|---|
| A provider's API or quirks | `client.py`, `anthropic.py`, `endpoint_profiles.py`, `retry.py`, `errors.py`, `gemma_tool_format.py`, `text_completion.py` | stays |
| A local runtime | `local_ml.py` (runtime half), `local_models/**` | stays |
| The KV-cache contract | `cached_call.py`, `kv_tracker.py`, `build_prefix`, the `TOOLS` dict *ordering* | stays |
| What the Director is told | `prompt_builder.py` prose (~510 lines) | **moves** |
| Which lorebook entries activate | `lorebook.py` (selection half) | **moves** |
| What autocomplete drafts look like | `local_ml.build_prompt` | **moves** |
| Group-chat visibility policy | `group_context.py` | weak; defer |

This test is worth reviewing on its own merits. If it is wrong, most of §4 is wrong.

---

## 3. Root cause (the finding that matters most)

`AGENTS.md:17` defines layer 5 as:

> `inference/` and `analysis/` — model access, **prompt assembly**, and pure text analysis

**"Prompt assembly" is the phrase that created the dump site.** It conflates two
unrelated things:

- **prefix assembly** — `build_prefix`, attachment embedding, the tools blob.
  Infrastructure. KV-critical. Correctly layer 5.
- **prompt authoring** — the actual words sent to the Director, Editor, and
  Writer. Product content, owned by whoever owns the behaviour.

Under the broad reading, every prompt string in the app is licensed to live in
`inference/`. That is how ~510 lines of pass prose ended up there.

Newer code already ignores the broad reading, deliberately and with comments
explaining why. `features/cards/public_profile.py:46`:

> `# Deliberately not registered in inference.tool_registry.TOOLS: that module`
> `# asserts PRE_WRITER_TOOLS | POST_WRITER_TOOLS == BUILTIN_TOOL_NAMES at import,`
> `# so registering here would force a turn-phase partition onto a tool that has`
> `# nothing to do with a turn.`

Same again in `features/cards/sheet_update.py:30`. `workflows/image_gen/prompts.py`
and `pipeline/passes/editor/length_guard.py` also keep their prompts local.

So the codebase already has two conventions in force, and the newer one is the
better one. **The primary recommendation is to narrow the `AGENTS.md` wording**
("prompt and tool-schema assembly for the shared cached prefix"). The file moves
in §4 are downstream of that decision. If a reviewer disagrees with narrowing the
rule, items 2–4 below should be rejected as a set, not individually.

---

## 4. Findings

### 4.1 `inference/lorebook.py` (413 lines) — same shape as the prose rewriter

`features/lorebook/` already exists, with real local modules (`changesets.py`,
`proposals.py`). But **15 of the ~28 names in its public facade are re-exported
straight out of `inference/lorebook.py`** (`features/lorebook/__init__.py:5-21`),
and `features/lorebook/proposals.py:10` imports four more from the same place.

Nothing in the module touches a client, a model, or a transport. It is keyword
scanning, entry-layer projection, and Markdown rendering. Even
`database/queries/worlds.py:273` documents the projection as
`inference.lorebook.select_effective_entries` — layer 6 pointing at layer 5 to
explain a lorebook concept.

**The one real constraint.** `workflows/toolkit.py:178` calls
`compute_constant_lorebook_block` to feed `build_prefix` inside
`build_offturn_prefix`. `workflows/` (4) sits *below* `features/` (3), so it
cannot import the feature slice.

This is structurally identical to the `workflows → passes.editor.audit` edge that
was previously resolved by extracting the `analysis/` layer — and that precedent
is live (`workflows/toolkit.py:7` imports `..analysis`).

**Proposed cut — the same cut already made for the prose rewriter:** block
*rendering* stays in `inference/` as the shared layer; selection, gating, the
Director catalog, and Dynamic Worlds move to `features/lorebook/`.

Note `build_offturn_prefix` exists to reproduce the turn prefix byte-for-byte for
off-turn workflow calls. Inverting the dependency (passing the block in) was
considered and rejected: it changes a KV-sensitive path and adds a parameter to a
workflow-author-facing API.

### 4.2 `prompt_builder.py` + `tool_registry.py` — pass content in the model layer

`prompt_builder.py` is 710 lines: roughly the first 198 are genuine inference
(`build_prefix`, `format_message_with_attachments`), and the remainder is
Director/Editor/Writer prompt prose — `DIRECTOR_PREAMBLE`,
`EDITOR_PATCH_INSTRUCTIONS`, `EDITOR_REWRITE_INSTRUCTIONS`, `WORLD_CHANGE_PREAMBLE`,
`build_editor_prompt`, `build_feedback_prompt`, `build_lorebook_select_prompt`,
and the style-injection block.

Consumers (verified, all of them):

| Symbol | Consumers |
|---|---|
| `build_director_tool_prompt`, `build_director_scene_step_prompt`, `render_direction_notes_block` | `pipeline/passes/director/director.py` |
| `build_lorebook_select_prompt` | `pipeline/passes/director/lorebook_select.py` |
| `build_direction_note_prompt` | `pipeline/passes/director/direction_note.py` |
| `build_editor_prompt`, `EDITOR_RENUMBER_NOTICE` | `pipeline/passes/editor/editor.py` |
| `build_feedback_prompt` | `pipeline/passes/editor/feedback.py` |
| `build_world_change_prompt` | `pipeline/passes/world_change.py`, `pipeline/world_proposal.py` |
| `compute_style_injection_block`, `resolve_mood_fragment_randoms` | `pipeline/passes/director/director.py`, `api/routes/conversations.py` |

Every consumer is in `pipeline/`, except two symbols also used by one API route.
The move target is unambiguous.

**`tool_registry.py` is a different case and must be treated separately — see §5.**
Its ~328 lines of concrete schemas look like the same problem, but the `TOOLS`
dict ordering is a cache contract. Recommendation: **move the prose, keep the
`TOOLS` dict literal centralized.**

### 4.3 `local_ml.py` — a runtime with one feature glued on

The llama-cpp-python machinery (load/lock/complete/score/classify) is genuine
inference and stays. Two things ride along that should not:

- **`build_prompt()` (`local_ml.py:368-401`)** — the autocomplete/typeahead prompt
  builder: character name, summary, recent messages, draft. Feature prompt
  assembly inside a runtime module. Sole production consumer:
  `api/routes/messages.py:499`.
- **The 39-line `if __name__ == "__main__":` block (`local_ml.py:404-442`)** —
  self-tests for `build_prompt`, `pov_from_logits`, and `pov_input`.
  `tests/unit/test_autocomplete.py` and `tests/unit/test_local_ml.py` already
  cover all three. Production code carrying duplicate test surface.

`GO_EMOTIONS` stays — the tuple order must match the GGUF head's logit order
(`local_ml.py:68`), which is a model fact. Worth noting that
`features/cards/expressions.py:14` imports it with an apologetic
`# dep-free tuple; no llama import`, which hints the label vocabulary would rather
be a datum than a runtime import. Low priority; not proposed here.

### 4.4 `group_context.py` (213 lines) — weak candidate, defer

Group-chat cast rendering and per-mode character-field visibility. Same domain as
`features/cards/public_profile.py`, and `docs/architecture/group-chats.md` treats
it as a feature. But there is no `features/group_chat/` slice to move into, and
`prompt_builder.py` consumes it directly.

Recommendation: **leave it.** Revisit only if 4.1 and 4.2 land and the seam
becomes obvious. Creating a feature slice to house one module is over-engineering.

### 4.5 Leftovers

- **`backend/inference/prose_rewriter/` still exists**, containing only stale
  `__pycache__` from the move. Untracked and gitignored, so it never appears in
  `git status` — but `ls inference/` still prints `prose_rewriter`, which is
  exactly the confusion the move was meant to end.
- Orphaned bytecode for deleted modules: `inference/__pycache__/isolated.pyc`,
  `local_completion.pyc`.
- **`roster_names` is exported from `inference/__init__.py:107` with zero
  consumers** — three internal call sites in `group_context.py`, no external
  import, no test import. A dead export, not dead code.

### 4.6 Structural note: the facade invites the dump

`inference/__init__.py` re-exports **60 names from 11 modules** into one flat
namespace. `from ..inference import (...)` therefore makes transport primitives
and product prose equally reachable at equal cost. There is no friction against
adding one more name, which is a contributing cause rather than a symptom.

Not proposing a facade rewrite — that is churn without behaviour change. But if
4.1–4.3 land, the facade should shrink with them rather than keep re-exporting
relocated symbols for compatibility.

---

## 5. Hard constraint: tool array order is cached-prefix bytes

**This is the constraint most likely to be missed by a reviewer, and violating it
would be a serious regression.**

`enabled_schemas()` (`tool_registry.py:430`) iterates `TOOLS` in **dict insertion
order**. Its output goes directly into `tools=tuple(...)` on `CachedBase`
(`pipeline/config.py:84`, `:95`), which `cached_call.py:100` documents as "the
cached bottom (prefix + tools + model)".

`kv_tracker._serialize_tools` uses `json.dumps(..., sort_keys=True)`, which sorts
object *keys* — **not** array order. Array order is therefore load-bearing for
prefix identity.

**Consequence:** scattering the seven built-in schemas into their pass modules
would make the tools-blob byte order depend on module import order, silently
invalidating the KV prefix for every existing conversation. KV cache is the
project's stated first priority. Do not do this.

Two further guards live in the same file and constrain any change:
`tool_registry.py:391` asserts `BUILTIN_TOOL_NAMES` matches `TOOLS.keys()`, and
`tool_registry.py:413` asserts the pre/post-writer sets partition it. The second
is the one `features/cards/public_profile.py:46` cites as its reason to stay out
of the registry.

**Acceptance test for any change in §4.2:** the tools tuple and prefix must be
byte-identical before and after. `kv_tracker` already computes this; assert on it
rather than eyeballing.

---

## 6. Recommended sequence

Ordered by risk, lowest first. Each step is independently shippable.

| # | Action | Risk | Notes |
|---|---|---|---|
| 0 | Narrow the `AGENTS.md:17` wording (§3) | none | **Decide this first.** Steps 2–4 are downstream of it. |
| 1 | `rm -rf backend/inference/prose_rewriter/`; drop `roster_names` from the facade | none | Independent of everything else. |
| 2 | Move `lorebook.py` selection/gating/catalog/Dynamic-Worlds → `features/lorebook/`; leave block rendering | low | Collapses the pass-through facade. Watch the `workflows/toolkit.py:178` edge. |
| 3 | Move `local_ml.build_prompt` → autocomplete feature; delete the `__main__` block | low | Smallest real win. |
| 4 | Move ~510 lines of pass prose out of `prompt_builder.py` into `pipeline/passes/` owners | **medium** | Largest diff. Prefix-adjacent — §5 acceptance test required. |
| — | ~~Scatter `TOOLS` schemas~~ | — | **Rejected**, §5. |
| — | ~~`pov_*` → `analysis/`~~ | — | **Rejected**, §8. |
| — | `group_context.py` | — | Deferred, §4.4. |

Steps 2 and 4 both touch `inference/__init__.py`; sequence them, do not parallelise.

---

## 7. Checked and cleared — do not redo

| Question | Finding |
|---|---|
| Layer violations out of `inference/`? | **None.** Only `..core` leaves the package; all other relative imports are intra-package. (First attempt used a regex blind to relative imports; re-run corrected, conclusion unchanged.) |
| Two local runtimes = duplication? | **No.** `local_ml` (in-process llama-cpp-python) and `llama_server/` (subprocess) are a deliberate split behind a `RuntimeKind` discriminator on `ModelSpec` (`local_models/catalog.py:49`, `:115`), sharing one catalog. |
| `endpoint_profiles.PROFILES` vs the "derive or degrade, never hand-keep a capability table" rule? | **Consistent.** The whole table is 2 endpoint keys (`api.deepseek.com`, `nano-gpt.com`) with 1 per-model override (`deepseek-reasoner`); unmatched → `None` → pass-through unchanged; entries encode *observed* rejections, not guessed capabilities. `anthropic.py:21,26` learns rejections at runtime, which is the derive pattern. |
| `inference/errors.redact` duplicating `core/utils.scrub_log`? | **No.** Unrelated: API-key removal vs. CWE-117 newline stripping. |
| `build_style_injection` dead? | **No.** Called internally at `prompt_builder.py:669`, directly tested in `tests/unit/test_interactive_fragments.py`. |
| `strictify_schema` misplaced with the schemas? | **No.** Consumed by `client.py` and `anthropic.py` — transport concern, correctly in layer 5. |

---

## 8. Withdrawn claims (calibration)

Claims made during this audit and retracted on verification. Listed so a reviewer
can weigh the surviving ones.

1. **`pov_input` / `pov_from_logits` → `analysis/`.** Withdrawn. Asserted their
   consumer was `workflows/image_gen/pov.py:70`; that line calls `aclassify_pov`,
   the async *runtime* wrapper. The pure helpers have no production consumer
   outside `local_ml` itself. Moving them would relocate two test files so that
   `local_ml` could import its own helpers back — over-engineering.
2. **Scattering `TOOLS` schemas into pass modules.** Withdrawn on discovering §5.
3. **`build_style_injection` is dead code.** False — the consumer grep excluded
   the defining file, hiding an internal call.
4. **"Every pass-prompt function has exactly one consumer."** Overstated; see the
   table in §4.2 for the two exceptions.
5. **`tool_registry.py:392` is the partition assert.** Wrong line; it is `:413`.
6. **`features/lorebook/__init__.py` is a "pass-through shell."** Overstated —
   15 of ~28 names, roughly half.
7. **"`PROFILES` has 3 per-model overrides"** (in a draft of §7). Miscounted: the
   regex conflated endpoint keys with model keys. Actual shape is 2 endpoints,
   1 model override. Corrected in place; the verdict was unaffected.

---

## 9. Open questions for review

1. **Is the §2 axis-of-change test the right principle?** It is the load-bearing
   assumption. Most of §4 collapses if it is wrong.
2. **Is step 4 worth its risk?** ~510 lines moved, prefix-adjacent, for a purely
   structural gain. A reasonable reviewer could accept steps 0–3 and reject 4.
   What would change the answer is whether the pass prose is expected to churn:
   if it is edited often, the move pays for itself; if it is stable, it may not.
3. **Should the shared lorebook rendering layer live in `inference/` or in
   `analysis/`?** §4.1 proposes `inference/` by analogy with the llama_server
   precedent. `analysis/` is arguably a better fit for pure rendering, and the
   `workflows → analysis` edge already exists.
4. **`kv_tracker._prev_turn_entries` grows without bound.** Module-level dict
   (`kv_tracker.py:13`), written at `:220` keyed by `conversation_id`, read at
   `:97`, with no `pop`, `clear`, or size cap anywhere in the module — verified.
   One retained entry per conversation for the process lifetime. This is a
   diagnostics cache, so the leak is slow and bounded by conversation count, but
   it is real. Out of scope for a placement audit; raised because it was found
   during it. Placement itself is fine — it is a cache-contract concern, layer 5.
5. **Is there a fifth tenant this audit missed?** Coverage was driven by the §2
   test applied to all 25 modules, but the test itself may have blind spots —
   particularly around `cached_call.py` and `kv_tracker.py`, which were accepted
   as infrastructure without deep reading.

---

## 10. How to re-verify

```sh
# §7 layer check (relative-import aware — the naive regex misses these)
grep -rnE "^\s*from\s+\.+(api|pipeline|features|workflows|database)\b" backend/inference/ --include="*.py"
grep -rhoE "^\s*from\s+\.[.a-z_]*" backend/inference/ --include="*.py" | sort | uniq -c | sort -rn

# §4.2 consumer table
for n in build_editor_prompt build_feedback_prompt build_world_change_prompt \
         compute_style_injection_block resolve_mood_fragment_randoms; do
  printf "%-32s " "$n"
  grep -rn "\b$n\b" --include="*.py" backend/ | grep -v __pycache__ \
    | grep -v "inference/prompt_builder.py\|inference/__init__.py" \
    | sed 's|backend/||;s|:.*||' | sort -u | tr '\n' ' '; echo
done

# §5 cache contract
grep -n "tools" backend/inference/cached_call.py
sed -n '78,96p' backend/pipeline/config.py
grep -n "^assert" backend/inference/tool_registry.py
```

Note: consumer greps must **not** exclude the defining file — that error produced
withdrawn claim 3.
