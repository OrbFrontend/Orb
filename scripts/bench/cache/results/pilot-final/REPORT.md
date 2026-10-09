# Bench 1 comparison pilot — 2026-10-09

The pilot attempted 18 turns across Orb and two custom TauriTavern configurations; 8 met the full workload checks. The 360-turn sweep has not run. These pilot failure rates describe this fixture and configuration, and do not establish an application speed ranking.

Each arm used three consecutive user turns at 2k and 32k nominal starting history, one repeat per size. llama-server restarted before each arm/size block. Starting histories were shared; later turns retained each application's actual history, including the effect of failed turns.

## Failure rates

Native failures mean Orb did not cleanly finish and save a reply, or TauriTavern did not reach native `completed`. Save mismatches separately count native-completed TauriTavern runs whose settled chat text differs from `output/main.md`. Workload qualification also requires exact final-file/chat agreement, direction fields, direction consumption, full history, wire settings, audit/patch/commit order, and required stages. A saved reply can therefore count as a workload failure. Rates use all attempted turns as the denominator; recovered tool errors are counted separately.

| Configuration / starting history | Attempts | Native terminal failures | Save mismatches | Workload failures | Native completed but unqualified |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb / pooled | 6 | 0/6 (0.0%) | 0 | 0/6 (0.0%) | 0 |
| Orb / 2000 tokens | 3 | 0/3 (0.0%) | 0 | 0/3 (0.0%) | 0 |
| Orb / 32000 tokens | 3 | 0/3 (0.0%) | 0 | 0/3 (0.0%) | 0 |
| TauriTavern handoff Profiles / pooled | 6 | 2/6 (33.3%) | 2 | 5/6 (83.3%) | 3 |
| TauriTavern handoff Profiles / 2000 tokens | 3 | 2/3 (66.7%) | 1 | 3/3 (100.0%) | 1 |
| TauriTavern handoff Profiles / 32000 tokens | 3 | 0/3 (0.0%) | 1 | 2/3 (66.7%) | 2 |
| TauriTavern single Profile / pooled | 6 | 1/6 (16.7%) | 3 | 5/6 (83.3%) | 4 |
| TauriTavern single Profile / 2000 tokens | 3 | 0/3 (0.0%) | 2 | 2/3 (66.7%) | 2 |
| TauriTavern single Profile / 32000 tokens | 3 | 1/3 (33.3%) | 1 | 3/3 (100.0%) | 2 |

5 save mismatches were whitespace-only: TauriTavern removed a trailing space before a newline or a final newline during its native commit. They remain unqualified under the harness's exact-text check, but are not native crashes or lost prose. This acceptance rule needs review before the full sweep.

A missing stage after an aborted run is an additional qualification finding, not a separate failed attempt. Cause tables can overlap and do not add up to the failure rate.

### Native error codes

| Configuration | Error code | Attempts with code |
| --- | --- | ---: |
| Orb | None recorded | 0 |
| TauriTavern handoff Profiles | `model.output_truncated` | 2 |
| TauriTavern single Profile | `model.output_truncated` | 1 |

### Workload and observer findings

| Configuration | Finding | Attempts with finding |
| --- | --- | ---: |
| Orb | None | 0 |
| TauriTavern handoff Profiles | `stages.invocation_sequence` | 3 |
| TauriTavern handoff Profiles | `completion.audit_commit_finish_order` | 3 |
| TauriTavern handoff Profiles | `stages.handoff_sequence` | 3 |
| TauriTavern handoff Profiles | `audit.missing` | 3 |
| TauriTavern handoff Profiles | `native.not_completed` | 2 |
| TauriTavern handoff Profiles | `completion.native_verification_failed` | 2 |
| TauriTavern handoff Profiles | `completion.final_file_chat_mismatch` | 2 |
| TauriTavern handoff Profiles | `draft.missing` | 1 |
| TauriTavern handoff Profiles | `editor.unresolved_findings_before_limit` | 1 |
| TauriTavern handoff Profiles | `response.reasoning` | 1 |
| TauriTavern single Profile | `completion.native_verification_failed` | 3 |
| TauriTavern single Profile | `completion.final_file_chat_mismatch` | 3 |
| TauriTavern single Profile | `direction.required.next_event` | 2 |
| TauriTavern single Profile | `editor.edit_batch_limit` | 1 |
| TauriTavern single Profile | `response.reasoning` | 1 |
| TauriTavern single Profile | `direction.required.keywords` | 1 |
| TauriTavern single Profile | `completion.audit_commit_finish_order` | 1 |
| TauriTavern single Profile | `draft.missing` | 1 |
| TauriTavern single Profile | `direction.missing` | 1 |
| TauriTavern single Profile | `native.not_completed` | 1 |
| TauriTavern single Profile | `audit.missing` | 1 |

## Work performed, including failed attempts

| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb | 23 | 4601 | 54400 | unavailable | 11 |
| TauriTavern handoff Profiles | 78 | 15520 | 68849 | 17 | 3 |
| TauriTavern single Profile | 83 | 10271 | 62304 | 15 | 11 |

`unavailable` means an observation was absent, not zero. Generated tokens include JSON and any reasoning the provider emitted. Orb's native log does not expose a complete failed-tool-call denominator here; its column is unavailable.

| Configuration / stage | Calls | Recorder seconds, summed | Generated tokens | Uncached input tokens |
| --- | ---: | ---: | ---: | ---: |
| Orb / director | 6 | 22.43 | 872 | 43930 |
| Orb / editor | 11 | 18.17 | 1130 | 8748 |
| Orb / writer | 6 | 23.75 | 2599 | 1722 |
| TauriTavern handoff Profiles / director | 12 | 59.94 | 4849 | 52011 |
| TauriTavern handoff Profiles / editor | 38 | 67.86 | 3076 | 10624 |
| TauriTavern handoff Profiles / writer | 28 | 76.52 | 7595 | 6214 |
| TauriTavern single Profile / single | 83 | 142.49 | 10271 | 62304 |

Recorder ingress-to-upstream-start overhead: median 0.558 ms, maximum 1.379 ms across 184 calls. This measures forwarding setup, not a direct-versus-proxy latency calibration.

Cache usage was cross-checked against provider timings in 184 calls; 0 differed. Details: [cache-crosscheck.json](cache-crosscheck.json). Native request associations: [association-crosscheck.json](association-crosscheck.json).

## Qualified observations

The following wall times are descriptive pilot observations only. They exclude forensic reads after native completion. TauriTavern includes real bridge work and polling wait; persisted terminal timestamps and observation delays remain in `turns.json`. First visible TauriTavern prose was not measured, so there is no cross-application first-prose comparison.

| Configuration / history / turn | Wall seconds | Full prompt tokens, maximum | Final words | Draft → final findings | Unflagged sentences preserved |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb / bellwick-2000 / 1 | 9.898 | 4600 | 356 | 6 → 0 | 19/20 (95.0%) |
| Orb / bellwick-2000 / 2 | 8.307 | 4969 | 353 | 4 → 0 | 32/32 (100.0%) |
| Orb / bellwick-2000 / 3 | 8.039 | 5412 | 304 | 5 → 2 | 22/22 (100.0%) |
| TauriTavern single Profile / bellwick-2000 / 3 | 18.539 | 9916 | 270 | 4 → 0 | 15/15 (100.0%) |
| Orb / bellwick-32000 / 1 | 20.573 | 35266 | 281 | 6 → 1 | 17/17 (100.0%) |
| Orb / bellwick-32000 / 2 | 9.104 | 35520 | 354 | 1 → 0 | 36/36 (100.0%) |
| Orb / bellwick-32000 / 3 | 11.080 | 36224 | 283 | 6 → 1 | 18/18 (100.0%) |
| TauriTavern handoff Profiles / bellwick-32000 / 1 | 48.555 | 39258 | 154 | 3 → 0 | 8/8 (100.0%) |

## Configuration and evidence

Orb application commit: `e78029f00af528375b117010607b2fcf0e3f1a45`. TauriTavern 2.3.0 application commit: `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375`. The pilot harness was uncommitted while running; source changes were confined to benchmark support.

Gemma 4 26B-A4B QAT UD-Q4_K_XL, SHA-256 `a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891`; llama.cpp `b1-edd6e2bb`, RTX 3090, 270 W power limit, Ubuntu 24.04. Both applications, recorder and MCP auditor ran on the inference host over localhost. TauriTavern used the real release binary and a software-rendered WebKitGTK WebView under Xvfb.

Samplers: temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1, max_tokens 4096, cache_prompt true, no seed. Thinking disabled in wire template flags and checked in responses. Server context 49152, one slot, 4096 MiB host cache, f16 KV, flash attention, no speculative decoding. TauriTavern budgets: 32 rounds / 80 calls per invocation, model retries 3 with 3000 ms interval; handoffs limited to 2 and total invocations to 3. Plans and skills disabled; full chat history included; each turn starts with empty Agent persisted memory. The single Profile has no handoff tool. The handoff Profiles share tool schemas and output/scratch root permissions.

Seven seeded Editor audits and 39 seeded phrase groups were shared through Orb's public analysis APIs. Negated narration and subject fixation were off. Auditor parity passed against the actual Orb Editor path; native MCP responses were replayed against their exact draft bytes and history.

Per-attempt data: [turns.csv](turns.csv), [turns.json](turns.json). Per-call costs and effective decoding constraints: [calls.csv](calls.csv), [calls.json](calls.json). Resume instructions and remaining validation: [HANDOVER.md](../../HANDOVER.md). Raw request/response bytes, native journals, drafts, saves, configurations, logs and server metrics are retained in the evidence archive identified in the handover.
