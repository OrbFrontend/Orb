# Benchmark plan: README numbers

Four benchmarks turn the README's four core claims into one number or chart each. Bench 1 compares Orb's pipeline with
TauriTavern's customizable agents on the same model and starting contexts. Bench 2 compares Director on with off;
Benches 3 and 4 score those turns. The results become a "By the numbers" section in the README.

| README claim | Benchmark | Headline result | Scored by |
| --- | --- | --- | --- |
| KV cache keeps multiple passes affordable | 1. Cache overhead and implementation comparison | Wall-clock time and uncached tokens vs context length: Orb, TauriTavern handoffs, and a single TauriTavern Profile (Gemma) | Common request recorder, provider usage, server timings, application events and saved output |
| Director solves directionlessness | 2. Driven replies | Difference in % of replies Jev labels driven, Director on minus off | Jev `choice` question, checked against 40 hand labels |
| Editor removes slop | 3. Slop before/after | Repair rate, held-out slop per 1,000 words, % of untouched prose kept | Auditor detectors, held-out phrase list |
| Built for small models | 4. Pass reliability | % of turns completed without warnings or errors; % of tool arguments that point at real ids | Turn stream, server log, saved tool calls |

A benchmark qualifies when no LLM judges prose quality and the result fits in a single README line or chart. One LLM,
Jev, labels reply structure for Bench 2. That is a classification, not a quality judgement, and the plan checks it
against hand labels before trusting it.

## Scope

- **No application source changes.** Benchmark drivers, a transparent localhost request recorder, and a small MCP
  auditor adapter may be added under `scripts/bench/`. Orb runs through its API; TauriTavern runs through its supported
  host API with the real prompt-assembly and chat-commit bridges. Observe streams, saved rows and files, application
  logs, upstream requests/responses and `/metrics`. Do not replace either application's orchestration or patcher.
- **Final numbers only.** Each benchmark reports per-turn outcomes. Bench 1 also reports per-call costs summed per
  stage and turn to explain cache reuse. Its optional frozen-request replay is a separate diagnostic, not a native
  turn or an application wall-clock result.
- **Chat transport only.** Text-completion mode is not benchmarked.
- **Status.** The Bench 1 harness is implemented under `scripts/bench/cache/`. The first comparison pilot (2026-10-09)
  under-counted TauriTavern: its checks were stricter for TauriTavern than for Orb, and its Profile prompts provoked a
  Gemma 4 tool-call failure. The fairness revision below fixes both; its results are in `scripts/bench/cache/results/`.

## Readiness (checked 2026-10-09)

| Item | State |
| --- | --- |
| Reference model on the box | `gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf` in `~/lmg/llama-cpp-webui/data/models/`; 14,249,047,104 bytes; sha256 `a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891`, matching Hugging Face |
| llama.cpp | `~/lmg/llama-cpp-webui/data/llama.cpp/build/bin/llama-server`, build `b1-edd6e2bb` (2026-10-03) |
| Fit | With `scripts/bench/serve_gemma.sh` flags at `-c 49152`: 15.8 GB of 24 GB VRAM in use |
| Cache behaviour | `scripts/bench/smoke_llama.py` passes at 8k and 32k: a new last user message reuses 35,888 of 35,904 tokens; `cache_prompt: false` reuses 0, so the smoke test's cold numbers are real cold prefills |
| Speed at 36k tokens | Cold prefill about 3,400 tok/s (11.3 s wall); warm request 1.0 s; decode about 138 tok/s |
| Box Orb checkout | `~/lmg/Anonymous/Orb` at `e78029f0`, tracked tree clean, `.venv` on Python 3.12 |
| Claude CLI | 2.1.294 on the Mac only. `--model claude-haiku-5-5` answers, and `modelUsage` lists only that model |
| Jev | `scripts/bench/driven/jev_check.py` labels 8/8 fixtures correctly; returned model `typesafe/jev-1.13-20260917` |
| Held-out list | antislop-sampler `slop_phrases_2025-04-07.json`, Apache-2.0, 2,500 phrases with counts |
| TauriTavern source review | `Darkatse/TauriTavern` at `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375` (`package.json` version 2.3.0); source inspected, not built or benchmarked |
| TauriTavern comparison readiness | Pending: Linux host on the box, isolated userdata, exported Profiles/preset, prompt/commit bridges, common recorder, auditor parity and stage-completion checks |

## Shared setup

### Where each part runs

- **Gemma runs, all four benches:** on the box (z@100.95.103.73, RTX 3090, 31 GB RAM). Orb, TauriTavern for Bench 1,
  the recorder, the auditor adapter and llama-server run there over `127.0.0.1`. A TauriTavern client on the Mac calling
  the box would introduce network and host differences, so it does not qualify for this comparison.
- **Haiku runs:** on the Mac, the only machine with a logged-in Claude CLI.
- **Scoring:** anywhere. It reads saved run folders, and Jev scoring needs the Jev key (see `jev_check.py`).

### A fresh Orb instance per run

`backend/database/connection.py` hard-codes `DB_PATH` to `backend/data/app.db` next to the source, and other runtime
data lives under `backend/data/` too. Isolate the run with a git worktree rather than a patched path:

1. `git worktree add <run-dir>/orb <commit>` from the main checkout. The worktree has an empty `backend/data/`, so
   `init_db()` builds the database from the seeds on first boot.
2. Start uvicorn from the worktree with the main checkout's `.venv` Python, on port 18899 so it never collides with a
   running Orb on 8899.
3. The backend and frontend in the worktree must match the recorded commit exactly. Check
   `git status --porcelain -- backend frontend` is empty before every reportable run. Pilot runs may use an uncommitted
   harness; reportable runs use a committed one.

Never start from `backend/data/app.db`: its prompts, phrase bank and settings are private, so a run built on it cannot
be reproduced by anyone else. Real chats are only used privately to check that the numbers hold on real use, and are
never committed.

### Pass switches

These were read from the code, not the settings UI, and they decide the arms:

| Pass | Runs when | Fresh-database default |
| --- | --- | --- |
| Any Agent step (Director, Editor, lorebook, state, ...) | `enable_agent` = 1 | 1 |
| Director | `enable_agent` and `enabled_tools.direct_scene` | on |
| Editor | `enable_agent` and `enabled_tools.editor_apply_patch` | **off** |
| Editor detectors | `editor_audit_toggles` | 7 of the 8 audit types on; `negated_narration` and `subject_fixation` off |

`enable_agent` turns off the Editor as well as the Director (`backend/pipeline/config.py`), so an arm that needs
"Director off, Editor on" toggles `enabled_tools.direct_scene` and keeps `enable_agent` at 1. The `enable_agent` field
on `POST /api/conversations/{cid}/send` is accepted but unused; only the setting counts.

### Settings snapshots

Two committed JSON snapshots, applied through `PUT /api/settings` and the endpoint and model-config routes. The harness
reads every value back with `GET /api/settings` and refuses to run if any differs.

**Base, in both snapshots:**

- Endpoint 1 URL `http://127.0.0.1:5000/v1`, chat mode. Bench 1 overrides it in every arm to the common recorder at
  `http://127.0.0.1:5001/v1`, which forwards unchanged to port 5000. Save and verify the applied endpoint override as
  well as settings. The URL lives on the endpoint record (`PUT /api/endpoints/1`), not in settings.
- `agent_same_as_writer` = 1. In single-model mode the Director and Editor inherit the Writer's model config,
  `extra_body` included (`backend/database/queries/settings.py`), so one `extra_body` controls every request.
- Writer model config: Orb's seeded samplers (temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1.0,
  max_tokens 4096). They are what a user gets.
- `reasoning_enabled_passes` all false. The harness also checks the outcome rather than trusting the setting: Gemma 4's
  chat template thinks by default, and a short test request with thinking left on spent its whole budget on reasoning.
  Every request must carry the thinking-off template flags, or the turn is rejected. Reasoning-channel output that
  appears anyway is reported per arm with its size, and its tokens count as cost; it does not disqualify a turn. Gemma 4
  can emit a bare `<|channel>thought` marker, or misplaced tool text, that llama.cpp routes to the reasoning field.
- `workflows_globally_enabled` = 0. The seeds ship it on, and a secondary workflow can change `messages.content` after
  the Editor.
- `length_guard_enabled` = 0, and decision, feedback and post-processing fragments off (all off in the seeds). No judge
  endpoint is configured.
- No `seed` in `extra_body`. On llama.cpp the same seed gives the same text only when nothing is reused from the cache.
  With cache reuse the logits shift slightly and the same seed gave different text in the smoke test, so a seed buys no
  reproducibility in any cache-on arm. Repeats are plain samples.

**`defaults.json` (Bench 1):** the base, plus `enabled_tools` = `{direct_scene: true, editor_apply_patch: true}`
and the seeded `editor_audit_toggles`. This is the out-of-the-box pipeline with the Editor turned on.

**`bench.json` (Benches 2–4):** `defaults.json`, plus:

- every Editor detector on: the 8 audit types (`banned_phrases`, `repetitive_openers`, `repetitive_templates`,
  `contrastive_negation`, `phrase_repetition`, `structural_repetition`, `anti_echo`, `negated_narration`) and the
  `subject_fixation` edit step;
- `agentic_lorebook_enabled` = 1 and the seeded `inventory` state fragment enabled, so the lorebook and state stages
  run and Bench 4 has ids to check.

### Reference model and server

- Gemma 4 26B-A4B, QAT `UD-Q4_K_XL` (file and hash above), served by `scripts/bench/serve_gemma.sh`:
  `-fit off -ngl 99 -c 49152 -np 1 -b 2048 -ub 512 -fa on -ctk f16 -ctv f16 -cram 4096 --jinja --metrics`.
    - `-fit off`: a model or context that does not fit fails, instead of llama.cpp quietly shrinking `-c` or moving
      layers to the CPU.
    - `-np 1`: with several slots, routing by prompt similarity can send a pass to a cold slot.
    - `-cram 4096`: the 8192 default can run a 32 GB host out of memory with Gemma (the box's own preset says so).
    - No `--swa-full`, default `--ctx-checkpoints` (32). Orb's reuse on sliding-window Gemma is built around
      checkpoints (`docs/architecture/kv-cache.md`), so this is the configuration users run. `--swa-full` is at most a
      sensitivity run.
    - No speculative decoding (MTP or draft model): it changes timing and is not what the README claims.
    - `-c 49152` leaves room for a 32k history plus system prompt, the 4096-token reply budget and the Editor's tail.
      Orb has no context-budget setting and sends the whole history, so an overflow is a hard error, not a silent trim.
- The script clears every `LLAMA_ARG_*` variable first, because llama-server reads them as flags.
- Nothing else may use the GPU during a run (the box also hosts ComfyUI). The harness checks `nvidia-smi` before each
  block. The card runs a custom undervolt and a 270 W power limit (`~/gpu.service`); record it as part of the machine.

### Haiku

- Claude transport (`claude-code://local`, `backend/inference/claude_code.py`) with the model name
  `claude-haiku-5-5`. Use the full id, not the `haiku` alias: the alias resolves to the same model today, but it moves
  when a new Haiku ships, and a run must not change model halfway.
- Before each run, a one-call preflight checks that the CLI's `modelUsage` lists exactly `claude-haiku-5-5`; record the
  CLI version.
- The CLI takes no temperature or seed and always runs with `--effort low`. Report the model as "Haiku 5.5 via the
  Claude Code CLI on its defaults"; do not claim reasoning is off.
- Expect rate limits: Bench 2 is 180 turns of 3–5 CLI calls each. Pace the run and record any retried turn.

### Corpus

- About 5 safe-for-work cards written for the benchmark, with 6 opening situations each: 30 contexts. Load them with
  `POST /api/characters` (or `/api/characters/import`).
- The cards must exercise the stages Bench 4 checks: each card carries lorebook entries and the seeded moods, so the
  lorebook and mood stages run.
- **Bench 1 histories** are generated once and frozen as fixtures. There is no chat-import route, so the harness
  writes them with the database layer's own `add_message`, then boots uvicorn. Shape them like real chats: replies of
  about 1,300 characters and user turns of about 100 (medians from real use). llama-server spends time rendering the
  template and tokenizing before a request reaches its slot, and that time grows with the number of messages: 6.4 s for
  36k tokens in 1,800 short messages, 0.5 s for the same tokens in realistic messages. Import the same role/content rows,
  card, persona and user script into TauriTavern. Measure the common history and each arm's full rendered requests with
  llama-server's `/tokenize`, not a character count. The native runs share starting history, then retain their own
  generated replies; they do not have identical later histories.

### Controls

- 3 repeats per cell.
- Bench 2 interleaves arms within a context (A, B, B, A ...), so slow drift never lines up with an arm. Bench 1
  counterbalances whole ten-turn blocks and restarts llama-server between arms; alternating applications within a
  block would disturb the single-slot cache state being measured.
- Every run folder records the Orb commit, harness commit, both settings snapshots as applied (read back from the API),
  the llama-server command line and build, the model hash, the Claude CLI version and the model ids it reported, Jev's
  returned model, the machine (GPU, power limit, RAM), and the date. Bench 1 also records the TauriTavern source/build,
  Profiles, preset, connection overrides, MCP tool schemas, userdata isolation, recorder and adapter revisions, and
  native stream/retry settings. Hash the effective tool blobs and save the actual request bodies.
- Save each turn's full event stream with an arrival timestamp on every event, and uvicorn's full log output.
  Bench 3's before text and Bench 4's warnings exist only in the stream and the log. Bench 1 measures model calls at
  the recorder and application phases from their own events; callback arrival times are not interchangeable across
  applications. Retain Orb's KV report (`provider: cached=X/Y tok`) as a cross-check of captured provider usage.

### Statistics

For Benches 2–4, repeats share a context, so they are not independent. Report 95% intervals from a bootstrap that
resamples the 30 contexts. Where both arms ran on the same contexts, bootstrap the paired difference.

Bench 1 resamples independent starting histories, paired across arms, retaining all repeats and consecutive turns
for each history together. Do not bootstrap its ten turns or repeats of one history as independent contexts, or reuse
the 30-context denominator from Bench 2. Three repeat blocks per size measure repeatability on the chosen fixture;
they do not support a population interval. Use the pilot to choose additional independent histories and repeats
before making a precise comparative README claim. With only one history per size, report descriptive results.

### Pilot first

Before Benches 2–4: 5 contexts × both arms × 1 repeat on Gemma, through the whole pipeline of run, save and score. It
shakes out the harness and gives a first look at Bench 2's effect size. Bench 1 has a separate comparison pilot below.
Size full runs only after their pilots; do not treat a source review as a completed pilot.

## Bench 1: Cache overhead and TauriTavern comparison

Measure wall-clock time to produce a directed, audited reply with Orb and equivalent custom TauriTavern agents, then
explain the result with uncached input, generated tokens and call counts. Keep cache enabled in every native arm.
Turning cache off would test llama.cpp's cache benefit, not the two applications' implementations. Gemma only: the
Claude CLI's own prompt and cache management would introduce a different transport.

### Source findings and workload mapping

Reviewed Orb at `e78029f00af528375b117010607b2fcf0e3f1a45` and TauriTavern at
[`a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375`][tt-commit]. These are source findings; actual rendered cache hits and
latencies still need measurement.

| Orb behavior | TauriTavern mapping | Boundary of equivalence |
| --- | --- | --- |
| Director fills scene fragments and moods | Director Profile writes `scratch/direction.md` as plain `field: value` lines, then calls `agent.handoff` for Writer | Carry the same enabled fragment instructions and allowed mood ids, in prose; validate the artifact independently. Gemma did not put custom fields into the handoff object, so the brief cannot carry the direction |
| Writer consumes direction and streams prose | Writer reads the direction file (`tt-single` already has it in its transcript), writes `output/main.md` with `workspace.write_file`, then hands off to Editor | Prose is generated in tool arguments, so file-tool costs remain part of its implementation |
| Editor audits and fixes the draft | Editor calls the shared auditor adapter; when it finds issues, reads the draft, applies `workspace.apply_patch` and re-audits; then commits and finishes | Same detector inputs and the same report text and fixing rules Orb's Editor reads; native exact-string patches differ from Orb's sentence-id batches and protected-sequence checks |

**Give TauriTavern idiomatic, well-formed prompts.** Gemma 4 writes tool arguments in its own syntax, delimiting
strings with a `<|"|>` token. When a prompt shows JSON examples, or asks for JSON inside a string argument, the model
can close an argument with a plain `"`; llama.cpp's lazy tool-call grammar then holds it inside the unterminated string
until `max_tokens`, and TauriTavern ends the Run as `model.output_truncated`. All three hard failures of the first
pilot were this trap. Profiles therefore describe arguments in prose and never ask for JSON content. Orb meets no
such risk: it forces each tool call and its arguments are short.
| Agentic lorebook selection | Supply a common catalog and record selected ids | Built-in `worldinfo.read_activated` reads the frozen activated entries, not Orb's selection catalog; excluded from Bench 1 |
| State fragments | Read/write `persist/` state files | File persistence does not supply Orb's operation validation or branch contracts; excluded from Bench 1 |

TauriTavern's [handoff preparation][tt-handoff] creates a new invocation with fresh messages and a fresh tool snapshot;
it does not inherit the caller's transcript. A receiver with `preset.mode = ref` requests prompt assembly from the
Run's frozen chat input. The fallback for other modes replaces messages with only its system instruction and task
brief. Require the explicit common preset for every handoff stage, and verify history inclusion from captured requests.

Within one invocation, its [tool loop][tt-loop] appends assistant messages and tool results, retaining a reusable
prefix. It uses `tool_choice = auto`; plain prose without tools is saved as a draft and triggers a recovery request,
rather than completing the stage. Profile plans currently [require `mode = none` and empty nodes][tt-validation],
so instructions alone do not guarantee Director → Writer → Editor execution. Check the actual artifacts and order.

Orb freezes the common prefix and tool list in [pipeline configuration](../../backend/pipeline/config.py).
Its Editor starts with the Writer request and draft, skips the model call for a clean audit, and makes at most three
edit iterations. In this benchmark's reasoning-off configuration, retries replace the draft and report at the tail;
only its reasoning-on path appends tool-call replay. Do not assume all Editor retries are append-only or always reuse
the previous draft's generated tokens. See [Editor implementation](../../backend/pipeline/passes/editor/editor.py).

### Three native arms

| Arm | Configuration | What it measures |
| --- | --- | --- |
| `orb` | `defaults.json`: Director, Writer, conditional Editor | Orb's current orchestration and native patching |
| `tt-handoff` | Director → Writer → Editor Profiles with a shared full-context preset | The closest separate-stage mapping using native handoffs and file tools |
| `tt-single` | One Profile performs direction → draft → audit → repair → commit → finish in one invocation | A control for TauriTavern's append-only transcript and avoided handoff work |

Both TauriTavern arms are custom benchmark configurations, not the shipped default Writer. Export them so the result
names exactly what was compared. Do not publish a generic claim about all TauriTavern configurations.

- **Common task.** Solo text chats, the same card/persona, enabled scene-fragment instructions, mood ids, seeded
  phrase bank, seven default audit types and scripted user turns. Disable agentic lore selection, state, subject
  fixation, length guard, feedback, post-processing, workflows and extra Skills/tools in all arms. Use the same static
  lore text, with any changing lore in the trailing context. Check `defaults.json` explicitly enforces these switches.
- **Connection and context.** Every Profile uses `preset.mode = ref` with the same named OpenAI/chat preset and
  `model.mode = connectionRef` with the same saved connection and exact model id. Set
  `context.initialChatHistoryMessages = -1` and enough preset context budget; this flag alone does not prevent
  PromptManager budget trimming. Verify all history rows and their order in every stage's captured messages. Match
  samplers, output budget, cache and thinking flags on the wire, using `custom_include_body` where needed.
- **Give TauriTavern a cache-conscious layout.** Keep stable card, persona and history before volatile stage
  instructions and `agentTask`; the preset controls those components' position and role. Use a user-role tail for
  changing instructions. The [default order][tt-order] places `agentSystemPrompt` before history, so leaving different
  stage instructions there is a different configuration. Publish the actual layout and inspect rendered message
  boundaries; byte overlap alone does not prove a Gemma restore-point hit.
- **Stable tool catalog.** The handoff Profiles use the same ordered tool allow-list, description overrides, visible
  and writable roots, and message-body path. Tool descriptions [depend on these fields][tt-tools]. The common list
  includes read/write/patch, commit/finish, handoff, and the auditor MCP tool. Because handoff is advertised, every
  such Profile must have `delegation.canHandoff = true`, including Editor. Set Writer's allowed caller to Director and
  Editor's to Writer, with `callable` and `allowAsHandoffTarget` true; keep Director non-callable so Editor has no
  eligible successor. Disable `agent.delegate`, allow only two handoffs and three invocations, and check Profile diagnostics.
  Compare effective wire schemas rather than assuming equal allow-lists produce equal schemas. `tt-single` omits
  handoff tools because it has no handoffs; its smaller catalog is part of that arm's native design.
- **Budgets.** Pin model-round, tool-call, retry and timeout budgets after the pilot; TauriTavern's tool budgets are
  per invocation, not a three-pass call count. Limit repair to three edit batches in its instructions and validate
  that count. Keep native recovery and control calls in the cost. Batch independent calls where the native runtime
  supports it; do not force one extra model request per file operation in the driver.

### Shared auditor and completion checks

Implement a small local MCP adapter that uses Orb's public `backend.analysis` helpers, the seeded phrase bank and
the exact default toggles. Reproduce the Editor's contextual audit: the newest 20 assistant replies, joined oldest
first with the draft, `structural_text = draft`, current user text for anti-echo, filtering findings back to the draft,
and building targets. Do not import module-private pipeline helpers; implement this boundary in benchmark code and
check it against the actual Editor on clean and flagged fixtures before reportable runs.

The adapter receives a stable logical draft path and resolves it to this Run's files through a driver-established
binding; it must audit the same draft bytes the Profile edits. Run identity, workspace access and startup ordering
are integration work still to validate. Avoid having the model copy the full draft into auditor arguments, which
would introduce an avoidable second generation of the prose. TauriTavern's [Skill scripts][tt-skill] run in QuickJS
without process/network APIs, so they cannot directly invoke Orb's Python auditor.

Both TauriTavern arms must audit before final commit and after each edit batch. Record remaining findings and why
editing ended. Identical detectors do not imply identical patch guards or stopping behavior: keep those differences
visible, and report repair/preservation alongside time. A clean Orb draft legitimately incurs zero Editor model calls;
TauriTavern's audit request, reads and commit/finish calls still count.

A qualifying task has a valid direction artifact consumed before the draft, an initial audit, re-audits after any
edits, and confirmed final output. Require the correct handoff sequence in `tt-handoff`. Apply the same contract to
every arm. TauriTavern's save path trims trailing whitespace (`cleanUpMessage`), so a saved reply that differs from
`output/main.md` only by that cleanup is intact. Every arm follows Orb's Editor stopping rule (with
reasoning off): after each re-audit, stop when it is clean, when no flagged sentence is left, or when the issue count
did not go down; at most three batches. Orb enforces it in code and TauriTavern Profiles are told it word for word.
Repair is scored at the audit where the rule stops, from the shared auditor's recorded audits; editing past it is
reported and stays in time and call counts. Findings left at that point are an outcome, reported for every arm. Read the direction that was in
force before the draft (the last one written), and accept list fields given as one delimited string. TauriTavern
Profiles get Orb's own mood wording ("the list of mood ids to activate; leave it empty for a neutral tone"): an empty
`moods:` line equals Orb's empty list, and a missing moods line is a failed direction. Retain the pre-edit draft
and final text for every arm. Distinguish a stage that ran but left findings from one skipped by the model. Report
all attempted turns, invalid/skipped stages, failures, partial output and timeout costs; show completed-task latency
with its completion rate rather than silently dropping failures or calling early exits speedups.

### Driver, clocks and measurements

Use [TauriTavern's host Agent API][tt-api], with the real frontend prompt-assembly and chat-commit bridges active.
A bare Rust loop bypasses part of the native implementation and does not qualify. Isolate userdata, Profiles and
chat fixtures, disable automatic pruning, and save Run journals, model responses, tool snapshots and draft versions.
Use a release build; record the host/WebView and build configuration.

The shared recorder forwards unchanged request and response bytes to llama-server. It records monotonic ingress,
first response data and stream end, actual messages/tools/constraints, provider usage and raw timing fields when
available. It must neither buffer the stream nor add context or decoding constraints. Check its overhead in the
pilot. Associate every request attempt with an Orb pass or TauriTavern invocation/stage, including recovery attempts.

1. **Native wall-clock:** trigger to confirmed final save and task completion. Orb ends at `done` after persistence;
   TauriTavern requires completed Run plus settled chat presentation. Start before prompt preparation in both drivers.
   Record backend completion and driver-observed completion separately when polling adds observation delay.
2. **Time to first visible prose:** observe Orb's first `token` and TauriTavern's first displayed `output/main.md`
   content. Exclude direction-file previews and raw tool JSON. TauriTavern streams workspace writes into the reply's
   chat message before the first commit; a page observer records each new text that message shows, and scoring takes
   the first one that is the start of the reply. Orb's time is taken at the client before any rendering.
3. **Model and stage time:** recorder request start to end for each call, summed per stage; application phase events
   separately include auditing, assembly, file work and persistence. Orb's send → first token is time to first prose,
   not Director duration; `writer_done` → `done` includes more than Editor inference.
4. **Uncached input:** prompt tokens minus provider cached tokens per call, summed by stage and turn. Missing cache
   fields mean unavailable, not zero. Cross-check Orb's KV log and llama-server's prompt-evaluation metrics/timings.
   Track the actual full prompt size, not just the nominal starting-history bucket.
5. **Work performed:** generated tokens including tool JSON/reasoning, prose words, model and tool calls, edits,
   retries, initial/final findings and unchanged unflagged sentences. Split clean drafts from repair-needed drafts.

TauriTavern's [event subscription][tt-events] polls every 500 ms by default. Use persisted event timestamps for its
phase boundaries, recorder clocks for model durations, and preserve actual host-bridge waiting as native overhead.
Do not charge polling notification delay to a model call or subtract real prompt/commit bridge work from turn time.

### Runs and interpretation

- **Comparison pilot first.** Validate Profiles, full-history inclusion, wire sampler/thinking parity, MCP auditor
  parity, artifacts, request-to-stage association and final saves. Then use one short and one long starting history
  with a few consecutive turns in all three arms to size budgets, fixtures and repeats. Runtime support on the box
  remains a prerequisite; source inspection is not evidence that this pilot passes.
- **Native sweep.** Starting histories of 2k, 8k, 16k and 32k tokens, ten scripted consecutive turns, three repeats
  per arm initially. Use the same starting fixtures and user script, retain each arm's real replies, and record
  resulting context growth and output length. Never replace native replies with canonical text while describing the
  run as consecutive-turn latency.
- **Block controls.** Restart llama-server before each `(arm, history, size, repeat)` block, clearing both slot and
  host-memory caches. Rotate three-arm block order across repeats. Report turn 1 separately from turns 2–10, but do
  not assume every first-turn stage is cold: earlier calls can warm later stages. No other application uses the GPU.
- **Sample size and runtime.** The initial native sweep is 120 turns per arm, 360 total; more independent histories
  or repeats may be needed for intervals. TauriTavern call counts are unknown, so the old under-an-hour estimate no
  longer applies. Estimate duration from the pilot, including failed attempts and restarts.
- **Optional mechanism diagnostic.** Replay frozen, valid request sequences with fixed direction, draft and tool
  results and a small common output cap to inspect prefill/cache behavior. Keep native messages, schemas and flags;
  do not splice Orb's prefix into TauriTavern's requests. Captured outputs are inputs for replay, not new successful
  tool actions. Reset cache per sequence and label this as frozen-request replay, separate from live application
  latency. A computed `full prompt tokens / cold prefill tok/s` is only a prefill estimate; it omits template,
  tokenization, decode and application work and is not the headline comparator.
- **Attribution.** Native wall-clock differences include cache reuse, generated JSON, duplicate draft reads, call
  counts, tool selection, patch behavior, bridges and persistence. All three arms use the same model, but its tool choices
  and output lengths can differ. Do not attribute the whole difference to KV cache or predict the winner from source.
  Orb's Director also pays to ingest the previous exchange and current user message; keep that input in its column.
- **Headline.** One comparison figure: native turn seconds and uncached input vs actual context size for all arms,
  with cold/steady-state results, completion rate, and a linked generated-token/call/repair breakdown. README wording:
  "On Gemma 4 26B-A4B at 32k starting history, directed and audited replies took X s in Orb, Y s with our TauriTavern
  handoff Profiles and Z s with its single-Profile configuration on an RTX 3090." State which latency statistic is
  used, name the custom configurations, and publish intervals only once enough independent histories support them.

[tt-commit]: https://github.com/Darkatse/TauriTavern/commit/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375
[tt-handoff]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src-tauri/crates/tt-application/src/services/agent_runtime_service/delegation/child_runtime.rs#L224
[tt-loop]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src-tauri/crates/tt-application/src/services/agent_runtime_service/loop_runner.rs#L136
[tt-validation]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src-tauri/crates/tt-application/src/services/agent_profile_service/validation.rs#L166
[tt-order]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src/scripts/PromptManager.js#L2432
[tt-tools]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src-tauri/crates/tt-application/src/services/agent_tools/registry.rs#L155
[tt-skill]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/docs/Agent/Skill.md#L71
[tt-api]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/docs/API/Agent.md
[tt-events]: https://github.com/Darkatse/TauriTavern/blob/a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375/src/tauri/main/api/agent-run-runtime.js#L145

## Bench 2: Driven replies

Show that turning the Director on raises the share of replies that change course partway through, not just ones that
tack a hook onto the end.

- **Arms.** `bench.json` in both, with `enabled_tools.direct_scene` on vs off. `enable_agent` stays 1 in both, so the
  Editor, lorebook and state steps run in both arms and Benches 3 and 4 can reuse these turns without skewing this one.
- **Turns.** Each of the 30 contexts ends on an open, passive user turn ("I take a seat and order a drink"), where a
  model left alone tends to just describe. 30 contexts × 2 arms × 3 repeats = 180 replies per model.
- **Scoring.** The Jev `choice` question in `scripts/bench/driven/jev_check.py` (`SHAPE`):

  | Label | Criterion |
  | --- | --- |
  | `driven` | Partway through, the reply changes course and develops the new direction. |
  | `afterthought` | The reply stays static and only adds a hook, event or question at the very end. |
  | `static` | The reply only reacts or describes, with no new direction. |

  Instruction: "Which describes how the reply moves the scene?" The state is `Current request:\n…\n\nReply:\n…`, the
  same shape as `gate_state` in `backend/pipeline/passes/editor/gate.py`. Score the final saved reply
  (`messages.content`, which equals `messages.writer_draft` once workflows are off). Report the share of each label and
  the mean probability of `driven`.
- **Before scoring.** Run `jev_check.py`. Every fixture must keep its label, and the returned model must still be
  `typesafe/jev-1.13-20260917`. If either changes, the judge has moved: stop and re-validate.
- **Keep the raw answers.** Jev is an alpha OpenRouter endpoint that can change or disappear. Commit every raw Jev
  response, keyed by a hash of state and question, so the charts can be rebuilt without calling Jev again.
- **Why this question.** A probe on 2026-10-09 showed that a plain yes/no "does the reply give the scene a new
  direction" scores end-of-reply hooks at 0.72–0.83, as high as real driven replies. The three-way question labeled all
  8 hand-written fixtures correctly at 0.85–0.99 (two of them about 1,600 characters long), matched a human reading of 8
  real replies, and moved by at most 0.02 on repeat runs.
- **Length check.** The Director may change reply length, and Jev's label may follow length. Report mean words per arm
  and the `driven` share within length bands.
- **Hand labels.** Label 40 replies from both arms without knowing which arm each came from. Sample them stratified by
  Jev's label, so that `afterthought`, which the probe never saw in real output, is represented. Report the 3 × 3
  confusion matrix, not just overall agreement.
- **Headline.** One stacked bar per arm per model showing the three labels, plus the paired difference in `driven` share
  with its interval. README line: "On open-ended user turns, X% of replies are driven with the Director on, Y% with it
  off." The condition stays in the line: the corpus was chosen to be where the Director should help.
- **Rule.** If the Director or a post-processing gate ever uses this question at runtime, the benchmark switches to a
  different wording, so the pipeline is not graded by the judge it was tuned against.

## Bench 3: Slop before and after the Editor

Show that the Editor removes what the auditor flags, adds nothing new, and leaves the rest of the draft alone.

- **No extra generation.** The Editor runs in both of Bench 2's arms, so all 180 turns per model give before/after
  pairs. Report by arm as well as pooled.
    - **Before:** the Writer's draft, joined from the `token` events received before `writer_done`. It is saved
      nowhere else.
    - **After:** `messages.writer_draft`, the text the Editor left, captured before any workflow
      (`backend/pipeline/orchestrator.py`). With workflows off it equals `messages.content`; the harness checks that.
- **Detectors.** All of `bench.json`'s, run by the scoring script through `backend.analysis`, the same code the Editor
  uses.
    - `phrase_repetition`, `structural_repetition`, `anti_echo` and `subject_fixation` read the chat history, not
      just the reply. Pass them the history the Editor saw.
    - `subject_fixation` is not one of the 8 audit types; it drives a separate LLM edit step. Its repair rate means
      running its detector on before and after.
    - `negated_narration` is default-off and its release still waits on held-out labels. Report it, and
      `subject_fixation`, apart from the other seven.
    - `banned_phrases` checks the 39-phrase seeded bank. Say so next to its number.
- **Held-out phrase list.** antislop-sampler's `slop_phrases_2025-04-07.json` (Apache-2.0, 2,500 phrases), pinned by
  commit hash. Drop every entry that fuzzy-matches the seeded phrase bank before scoring. This is the only measure the
  Editor never sees. Report raw counts alongside the rate, because Gemma's hits on a list built from other models'
  output may be sparse.
- **Measures.**
    1. Repair rate per detector: % of draft findings gone from the final reply, with raw counts, since some detectors
       will be in single digits.
    2. Introduced findings: findings in the final reply that were not in the draft, per 1,000 words.
    3. Held-out drop: change in held-out slop per 1,000 words between draft and final.
    4. Preservation: % of unflagged sentences left byte-identical, split with the Editor's own sentence splitter (the
       measure from the sentence-ID benchmark).
    5. Length change in words, and the share of flagged sentences removed rather than rewritten. Deleting a flagged
       sentence counts as a repair, so this keeps the repair rate honest.
- **Headline.** A before/after bar per measure. README line: "The Editor fixes X% of flagged slop, cuts held-out slop by
  Y%, and leaves Z% of untouched prose byte-identical."
- **Rerun candidate.** On 2026-08-07, sentence-ID patching raised end-to-end repair from 93.6% to 99.9% across 7 models.
  That harness was never committed, so the number cannot be reproduced until a committed harness reruns it.

## Bench 4: Pass reliability

Measure completed turns without reported failures and grounded tool arguments on both target models. This does not
establish that every eligible pass ran while silent skips remain uncounted.

- **Source.** Bench 2's turns. Nothing new is generated.
- **Record the constraint actually used.** Orb's localhost Gemma chat path sends native `tools` and `tool_choice`;
  it does not automatically convert every forced call to a strict `response_format.json_schema`. That conversion
  depends on endpoint policy or `tools_in_prompt = false` (`backend/inference/client.py` and `endpoint_profiles.py`).
  A per-call `json_schema` supplied by a pass can be discarded on the native-tool path; server-side tool grammar
  enforcement is a separate fact to verify. The Claude transport uses the CLI's `--json-schema`. Save the effective
  request/constraint for each transport and distinguish application validation from decoding constraints. Include
  observed parse/schema failures where available; do not assume 100% validity by construction or make it the headline.
- **Measures.**
    1. Clean completed turns: % of all attempted turns ending in `done` with a confirmed non-empty final reply and no
       `warning` or `error`. Treat truncated streams and timeouts as unfinished, not clean. Count attempts from the
       saved streams, not messages: a Writer failure before prose saves no reply, but failure after some prose can
       save partial output through fallback persistence (`backend/pipeline/persistence.py`). Neither is a clean turn.
    2. Failures by stage, from uvicorn's log. The stream thins warnings (`reported_once` in
       `backend/pipeline/failures.py`): once a step or a cause has been reported, later warnings with the same cause are
       dropped, so one outage shows up under a single stage. The log keeps every failure.
    3. Grounded arguments: % of saved tool calls in `conversation_logs.tool_calls` whose references exist: moods,
       lorebook entry ids, Editor sentence ids. Sentence ids are free strings in the schema, not an enum, so this is
       not grounded by construction. The Editor rejects an unknown id and asks again, so report first-try grounding
       apart from final grounding. Also check `conversation_logs.state_report` for state operations the turn rejected.
- **Coverage.** Director reliability rests on the 90 Director-on turns per model, and every turn runs `bench.json`
  (every detector on, the lorebook and one state fragment enabled), not the out-of-the-box settings. Say both next to
  the result.
- **Limit.** A pass the model skips without failing leaves no trace in the stream or the saved calls, so skips are not
  counted. State this next to the result.
- **Headline.** A model × stage table. README line: "On Gemma 4 26B-A4B, X% of N attempted turns completed without
  reported warnings or errors, and Y% of recorded tool references pointed at real ids on the first try." Link the
  settings and silent-skip limit; do not rewrite clean-turn rate as proof that every pass completed.

## Order of work

Bench 2 produces the turns that Benches 3 and 4 score, so it runs right after the shared harness.

1. Shared harness: worktree boot, snapshot apply-and-verify, turn driver that saves timestamped streams and the log,
   run-folder manifest. Corpus and frozen Bench 1 starting histories. For Bench 1, also build the common recorder,
   shared auditor MCP adapter and TauriTavern host driver; isolate userdata and export the three-arm configurations.
2. Bench 2 pilot (5 contexts, Gemma), through scoring. Separately validate Bench 1's auditor parity, Profiles,
   history inclusion, prompt/commit bridges, clocks and completion checks; then run its short/long comparison pilot.
3. Bench 1 on the box after its pilot: counterbalanced native blocks for all three arms, with server restarts per
   block. Optional frozen-request replay and cold-prefill estimates are separate diagnostics. It can run while the
   Bench 2 hand labels are done; select runtime and sample size from the comparison pilot.
4. Bench 2 on Gemma (box) and Haiku (Mac), then Jev scoring and the 40 hand labels.
5. Bench 3, on Bench 2's saved streams and replies.
6. Bench 4, on Bench 2's saved streams, logs and `conversation_logs`.

## README output

A short "By the numbers" section under Design Principles:

- Four lines, one headline per benchmark, each linking to its folder under `scripts/bench/`.
- Two figures: Bench 1's three-arm wall-clock/cache comparison and Bench 2's stacked bars, rendered by the scoring
  scripts into `docs/assets/`. Link Bench 1's completion, output-length, call-count and repair breakdown.
- One footnote line giving the models, transports, machine, llama.cpp build, Orb and TauriTavern commits and exported
  benchmark configurations, so anyone can rerun it.

## Files so far

| File | Purpose |
| --- | --- |
| `scripts/bench/serve_gemma.sh` | Starts llama-server with the pinned flags; extra flags append for sensitivity runs |
| `scripts/bench/smoke_llama.py` | Standard-library check of checkpoint reuse, `cache_prompt: false` and prefill speed on the inference host |
| `scripts/bench/driven/jev_check.py` | The benchmark question, the 8 fixtures, and the pre-scoring check that Jev still labels them correctly |

## Deferred

| Item | Why it waits |
| --- | --- |
| Long-chat degradation curve (repetition and length drift over 60–100 turns) | Needs a scripted user that stays believable for 100 turns; probably the strongest chart once that exists |
| Cost per turn on API providers | A table, not a claim. The KV report already logs per-call tokens, so it is cheap to add later |
| Tool-call skip rates | A skipped pass leaves no trace; counting skips needs the turn log to record per-call outcomes, which is a source change |
| A synthetic naive-prompt Orb arm | The primary comparator is now TauriTavern's native custom agents. A deliberately naive Orb layout needs application changes and would not establish a real-project wall-clock comparison |
| `--swa-full` sensitivity run for Bench 1 | Not the configuration users run; worth one extra curve only if a reader asks |
| Text-completion transport | Out of scope for this round |
| Super-regenerate variety | No README claim to back yet beyond "mileage varies" |
| Blind A/B of writing quality judged by people | The only honest measure of "better writing", but needs raters |
| Fragment chaining and the Editor's multi-step loop | Internal design questions, not README material |
