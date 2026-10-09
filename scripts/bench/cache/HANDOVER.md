> **Superseded.** This hands over the first pilot. Its acceptance rules were stricter for TauriTavern than for Orb, and its Profile prompts provoked a Gemma 4 tool-call trap; both were fixed afterwards. See [README.md](README.md) and the plan's Bench 1 status note.

# Orb / TauriTavern benchmark handover — 2026-10-09

The current beat is complete: the native Bench 1 harness runs both applications on the inference host, and its final short/long pilot has been scored. The user requested handover before the 360-turn sweep. That sweep has **not started**; no README speed claim or comparison figure has been published.

Read the [pilot report](results/pilot-final/REPORT.md) first. It contains failure rates, causes, all-attempt costs, and descriptive qualified observations. The governing plan remains [docs/plans/benchmarks.md](../../../docs/plans/benchmarks.md). Only its Orb/TauriTavern comparison was attempted; Benches 2–4 remain outside this beat.

## Results and acceptance rule

Final experiment: `comparison-pilot-final`, three consecutive scripted user turns at both 2k and 32k starting history, one repeat, three configurations, **18 attempts**.

| Configuration | Attempts | Native terminal failures | Exact-save mismatches | Full workload failures | Qualified turns |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb | 6 | 0/6 (0.0%) | 0 | 0/6 (0.0%) | 6 |
| TauriTavern handoff Profiles | 6 | 2/6 (33.3%) | 2 | 5/6 (83.3%) | 1 |
| TauriTavern single Profile | 6 | 1/6 (16.7%) | 3 | 5/6 (83.3%) | 1 |

There were 15 native terminal completions, 10 replies passing the exact final-file/chat verification, and 8 fully qualified turns. In saved JSON, legacy `complete` means **verified saved reply**, not simply native terminal status. New fields `native_terminal_completed` and `verified_saved_reply` make the distinction explicit.

All five save mismatches were whitespace-only: four removed a space before a newline; one removed a final newline. Native TauriTavern calls `script.cleanUpMessage` before `saveReply` in `src/tauri/main/api/agent-chat-message.js:5`. They are retained as failures of the harness's exact-text acceptance check, **not described as crashes or lost prose**. Review whether the plan permits this documented native cleanup before freezing acceptance for the full sweep. Do not silently relax the rule or relabel historical results.

Other workload findings include missing Editor/handoffs/audits after aborted runs, missing direction fields, an extra edit batch, stopping with audit findings before the three-batch limit, and reasoning emitted in two calls despite disabled wire flags. The three hard native errors were `model.output_truncated`. Native `run_failed` can carry the root invocation scope even when the failing call belonged to Writer; use recorder/native attempt association rather than assuming that scope identifies the causal stage.

All **184** final-pilot model calls retained exact request/response bytes. Cache usage matched provider `cache_n`/`prompt_n` timings in all 184. Of 161 TauriTavern calls, 158 were associated by both response ID and persisted attempt timestamp; the three truncations used persisted attempt timestamps because native response-storage events were absent. Orb's 23 calls were associated by native forced `tool_choice` or Writer `none`. Full ordered history and sampler parity passed for every final-pilot call. Orb draft captures matched raw Writer response content exactly. Successful native MCP audits replayed with exact-byte/contextual parity.

## Files and evidence

| File | Purpose |
| --- | --- |
| [defaults.json](defaults.json) | Fresh seeded Orb settings, Editor on, benchmark overrides |
| [fixtures/](fixtures/) | Frozen 2k/8k/16k/32k histories, card, persona, ten user turns |
| [../recorder.py](../recorder.py) | Transparent unbuffered localhost request/response recorder |
| [../auditor.py](../auditor.py) | Stateless MCP adapter using public Orb analysis APIs and seeds |
| [orb_driver.py](orb_driver.py) | Native API setup/send, public DB seeding/readback, SSE observer |
| [tauri_driver.py](tauri_driver.py) | Native release WebView through tauri-driver |
| [tt_setup.js](tt_setup.js) | Saved preset, connection, MCP registration and four Profiles |
| [tt_run.py](tt_run.py) | Real start/prompt/commit bridges, journals and settled presentation |
| [blocks.py](blocks.py) | Cold server restarts, isolated Orb worktrees, rotated blocks |
| [inspect.py](inspect.py) | Independent post-run workload checks, audits, stage/cost accounting |
| [report.py](report.py) | Rebuilds pilot Markdown, CSV and JSON without inference |
| [results/pilot-final/](results/pilot-final/) | Final report, turn/call data and cache/association checks |
| [evidence/bench1-pilot-evidence-20261009.tar.gz](evidence/bench1-pilot-evidence-20261009.tar.gz) | Raw pilot/setup evidence; archive checksum and index are beside it |

The archive contains all `comparison-pilot*` experiments, early standalone pilots/setup failures, shared recorder traffic and auditor snapshots, native workspace journals, exported configurations, generated Orb databases, logs, metrics, provenance and a harness snapshot. Application source worktree copies were omitted; pinned commits reproduce them. No private main Orb database was read or copied.

Archive: 11,170,585 bytes, 7,129 entries, SHA-256 `c597a6ba7abe57d63aa70de10d85a0e5634e613b225d044f21c37d73e86eaab9`. See [evidence-manifest.json](evidence/evidence-manifest.json) for the complete index, [provenance.json](evidence/provenance.json) for build/machine hashes, and [services-stopped.json](evidence/services-stopped.json) for cleanup verification.

Earlier iterations are **diagnostic evidence**, not pooled with final results. Their wire settings, Profiles and/or observers changed. `comparison-pilot-r2` has 18 attempts; its Orb SSE observer stripped whitespace or failed to decode native newline escapes, so its preservation/audit numbers are not valid. `comparison-pilot` also preserves an observer failure on numeric text. Early TauriTavern pilots include thinking-on/wrong sampler configurations and interrupted tasks. Keep their raw evidence; do not count them as successful comparable turns or infer application failure rates from setup attempts.

The final pilot's harness was uncommitted during execution, as the plan allows for pilots. All local additions are uncommitted. Before reportable runs, review and commit a stable harness, record its commit, and check both application source trees against the pinned application commits. There is no benchmark commit to cherry-pick yet.

## Host and stopped services

Inference host: `z@100.95.103.73`. Root: `/home/z/lmg/orb-benchmark-20261009`.

| Item | Location / state |
| --- | --- |
| Orb source / shared Python | `/home/z/lmg/Anonymous/Orb`, `.venv/bin/python` (3.12) |
| Isolated harness worktree | `ROOT/orb-pilot`, Orb application commit `e78029f00af528375b117010607b2fcf0e3f1a45` |
| TauriTavern source | `ROOT/tauritavern`, commit `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375`, version 2.3.0 |
| Built native release | `ROOT/tauritavern/src-tauri/target/release/tauritavern` |
| Isolated native data | `ROOT/tt-pilot-data/com.tauritavern.client/data` |
| WebView configuration | `ROOT/tt-pilot-config`; Xvfb `:97`, software rendering |
| Shared raw recorder/auditor evidence | `ROOT/pilot/requests`, `ROOT/pilot/audits` |
| Final scored experiment | `ROOT/comparison-pilot-final` |
| Report / provenance / cleanup | `ROOT/reports/pilot-final`, `ROOT/provenance.json`, `ROOT/services-stopped.json` |

All benchmark-owned services were stopped: Orb port 18899, llama-server 5000, recorder 5001, auditor 5002, tauri-driver 4444 and Xvfb `:97`. PID files remain for provenance and may be stale; verify `/proc/PID/cmdline` before signaling anything. No GPU compute process remained after cleanup. The main user's Orb/ComfyUI processes were not touched.

SSH control socket used: `/private/tmp/orb-bench-ssh-%C`. It may still work with `ssh -S /private/tmp/orb-bench-ssh-%C z@100.95.103.73 ...`. If expired, use the credential the user supplied in the conversation interactively; it was not stored in files or command text.

TauriTavern was built successfully with Node 24.19.0 (`~/.nvm/versions/node/v24.19.0/bin`), pnpm 11.22.0, Rust 1.99.0 and tauri-driver 2.1.0 (`~/.cargo/bin`). WebKitGTK 2.52.6 and Xvfb are installed. Full OS/GPU/dependency versions, native binary hash, model hash and harness file hashes are in provenance. The native endpoint trust grant for `http://127.0.0.1:5001/v1` is already saved in the isolated data directory.

## Resume commands

Run these on the inference host from `ROOT/orb-pilot`. The root and Python variables below are task-specific; do not overwrite `HOME` or `CODEX_HOME`.

```sh
BENCH_ROOT=/home/z/lmg/orb-benchmark-20261009
BENCH_PYTHON=/home/z/lmg/Anonymous/Orb/.venv/bin/python
cd "$BENCH_ROOT/orb-pilot"

nohup "$BENCH_PYTHON" -m scripts.bench.recorder \
  --output "$BENCH_ROOT/pilot/requests" \
  --binding "$BENCH_ROOT/pilot/recorder-binding.json" \
  > "$BENCH_ROOT/pilot/recorder.log" 2>&1 &
echo $! > "$BENCH_ROOT/pilot/recorder.pid"
nohup "$BENCH_PYTHON" -m scripts.bench.auditor \
  --output "$BENCH_ROOT/pilot/audits" \
  --binding "$BENCH_ROOT/pilot/auditor-binding.json" \
  > "$BENCH_ROOT/pilot/auditor.log" 2>&1 &
echo $! > "$BENCH_ROOT/pilot/auditor.pid"
nohup Xvfb :97 -screen 0 1280x800x24 \
  > "$BENCH_ROOT/pilot/xvfb.log" 2>&1 &
echo $! > "$BENCH_ROOT/pilot/xvfb.pid"
nohup env DISPLAY=:97 \
  XDG_DATA_HOME="$BENCH_ROOT/tt-pilot-data" \
  XDG_CONFIG_HOME="$BENCH_ROOT/tt-pilot-config" \
  WEBKIT_DISABLE_COMPOSITING_MODE=1 LIBGL_ALWAYS_SOFTWARE=1 \
  /home/z/.cargo/bin/tauri-driver --port 4444 \
  > "$BENCH_ROOT/pilot/webdriver.log" 2>&1 &
echo $! > "$BENCH_ROOT/pilot/webdriver.pid"
```

Check those ports/processes before launching; the controller starts/restarts llama-server and isolated Orb itself. A fresh pilot output directory is mandatory. Completed blocks can be resumed; an existing incomplete block currently raises instead of replacing evidence.

```sh
"$BENCH_PYTHON" -m scripts.bench.cache.blocks \
  --root "$BENCH_ROOT" --output "$BENCH_ROOT/comparison-pilot-next" \
  --pilot --sizes 2000 32000 --turns 3 --repeats 1
```

Offline final-pilot scoring/rebuild (services need not run):

```sh
"$BENCH_PYTHON" -m scripts.bench.cache.inspect \
  --runs "$BENCH_ROOT/comparison-pilot-final" \
  --requests "$BENCH_ROOT/pilot/requests" \
  --audits "$BENCH_ROOT/pilot/audits" \
  --applied "$BENCH_ROOT/pilot/orb-short/applied.json"
"$BENCH_PYTHON" -m scripts.bench.cache.report \
  --runs "$BENCH_ROOT/comparison-pilot-final" \
  --output "$BENCH_ROOT/reports/pilot-final"
```

These commands also work locally after unpacking the evidence archive into a temporary directory and changing the four evidence paths. Run the modules from this repository so their pinned fixture/analysis imports resolve. `report.py` intentionally handles pilots only; extend it for a full sweep rather than describing full runs as pilot data.

## Next work

1. Resolve and document native whitespace-cleanup acceptance. Inspect TauriTavern's remaining task failures and decide whether to adjust its custom instructions/budgets; keep identical ordered tools across handoff Profiles and preserve native orchestration. Repeat the short/long pilot if configuration or acceptance changes. There was only one fully qualified TauriTavern turn per configuration, not a qualified consecutive-turn sequence.
2. Finish remaining comparison calibration: direct-versus-recorder overhead, independent rendered-request token checks through `/apply-template` + `/tokenize`, and Orb KV log/server `/metrics` cross-checks. Provider usage/timing parity and byte-preserving recorder tests already pass. Equivalent first-visible-prose measurement is currently unavailable and should stay unavailable unless implemented meaningfully.
3. Commit a stable harness and enrich `blocks.py` manifests with the harness commit, fixture hash, model/build/binary provenance and exported configuration identities. Current manifests record application commits, arm, size, repeat, pilot flag and timing; separate provenance holds the remaining fields. Pin the full-sweep acceptance and report schema before running it.
   Harden the remaining failure paths before that commit: `inspect.py` currently assumes Orb logs/database readbacks exist and parses response SSE strictly, so an early Orb HTTP failure or a malformed/truncated SSE frame can still interrupt scoring. Preserve/count those attempts rather than fabricating missing artifacts. TauriTavern's 600-second polling deadline starts after the launch bridge returns; WebDriver's launch-script timeout is 900 seconds, so the whole trigger is not yet bounded to 600 seconds. A WebView constructor failure also needs guaranteed session cleanup. These paths were not exercised by the final pilot's complete HTTP streams.
4. Run 2k/8k/16k/32k × 10 turns × 3 repeats × 3 arms = **360 attempts**, with a cold server per block and rotated arm order. Do not replace native failed replies with canonical text. Keep all failures and all their costs. Estimate runtime using the pilot, including restart/assembly overhead; it is not an under-an-hour promise.
5. Build the requested three-arm latency/uncached-input figure with turn 1 separated from turns 2–10, completion/failure rates beside it, and linked generated-token/call/repair data. Use actual context growth and output lengths. There is only one independent frozen history per bucket, so do not publish unsupported intervals or attribute the whole wall-time difference to cache.
6. Add the plan's narrowly worded README result only when enough qualified reportable data supports it. No generic TauriTavern speed/writing-quality claim is established by this pilot.

## Validation and workspace state

- Benchmark tests: **15 passed**, including real Editor-boundary auditor parity, exact-byte MCP snapshots, transparent/truncated streaming, absent-cache handling, history roles/order, malformed directions, and numeric/whitespace/newline SSE captures.
- Evidence archive SHA-256 verified after download. Independent local offline scoring and report rebuild reproduced **18 attempts / 15 native terminal completions / 10 verified saves / 8 qualified turns**. All handover/report artifact links resolve. The three native JavaScript snippets parse correctly as async host-script bodies.
- Repository validation: backend/frontend formatting passed; lint passed, Pyright **0 errors**, layer checks passed; frontend **634 tests passed**; backend/integration **4860 passed, 1 skipped**. The first sandboxed full test run hit loopback bind permission errors; the permitted rerun passed.
- Only benchmark support and result/documentation additions were made. Tracked application source diff is empty. `docs/plans/` and the three original support scripts were already untracked and were preserved.
- An unrelated `:memory:.ses` file is untracked at the repository root; it was left alone. No source commit, branch merge, publication or external message was made.
