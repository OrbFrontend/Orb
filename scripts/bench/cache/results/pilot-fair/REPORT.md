# Bench 1 comparison pilot

18 attempted turns across Orb and two custom TauriTavern configurations; 16 met the shared task contract. Starting histories: 2,000, 32,000 nominal tokens; 3 consecutive scripted user turns per block; 1 repeat block(s) per size and arm. llama-server restarted cold before every block, and block order rotated across repeats. Starting histories were shared; later turns kept each application's own replies, including the effect of failed turns.

There is one frozen history per size, so these are descriptive results for this fixture and these configurations, not population intervals and not a claim about every TauriTavern configuration.

## Task contract

Every arm must produce a valid scene direction before its draft and use it, audit the draft with the same detectors, re-audit after every edit batch, and save the final reply intact. TauriTavern's native save cleanup (trailing whitespace) counts as intact. How editing ended (findings left, more than three batches), reasoning-channel output and save cleanup are reported below as observations for every arm; they do not disqualify a turn. Thinking is disabled on the wire in every request and verified there.

## Completion

| Configuration / starting history | Attempts | Native failures | Qualified |
| --- | ---: | ---: | ---: |
| Orb / all | 6 | 0/6 (0.0%) | 6/6 (100.0%) |
| Orb / 2,000 | 3 | 0/3 (0.0%) | 3/3 (100.0%) |
| Orb / 32,000 | 3 | 0/3 (0.0%) | 3/3 (100.0%) |
| TauriTavern handoff Profiles / all | 6 | 0/6 (0.0%) | 6/6 (100.0%) |
| TauriTavern handoff Profiles / 2,000 | 3 | 0/3 (0.0%) | 3/3 (100.0%) |
| TauriTavern handoff Profiles / 32,000 | 3 | 0/3 (0.0%) | 3/3 (100.0%) |
| TauriTavern single Profile / all | 6 | 1/6 (16.7%) | 4/6 (66.7%) |
| TauriTavern single Profile / 2,000 | 3 | 0/3 (0.0%) | 2/3 (66.7%) |
| TauriTavern single Profile / 32,000 | 3 | 1/3 (33.3%) | 2/3 (66.7%) |

## Latency and cost per turn

Wall time runs from the driver's trigger to Orb's `done` after persistence, or to TauriTavern's completed Run with its chat presentation settled. Medians over qualified turns; turn 1 follows a cold server start and is reported apart from turns 2–10. First visible prose is Orb's first streamed Writer token at the client, and TauriTavern's first reply text rendered in its chat message (checked against the start of the reply). Uncached input, calls and generated tokens are medians over all attempts, failures included.

| Configuration | Start | Turns | Qualified | Wall s | First prose s | Uncached input | Model calls | Generated tokens | Prompt tokens, max |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Orb | 2,000 | turn 1 | 1/1 | 7.5 | 3.1 | 5,181 | 3 | 688 | 4,402 |
| Orb | 2,000 | later turns | 2/2 | 8.7 | 2.1 | 3,156 | 4 | 844 | 5,160 |
| Orb | 32,000 | turn 1 | 1/1 | 21.0 | 13.5 | 36,730 | 4 | 812 | 35,320 |
| Orb | 32,000 | later turns | 2/2 | 10.2 | 3.6 | 2,700 | 4 | 723 | 35,864 |
| TauriTavern handoff Profiles | 2,000 | turn 1 | 1/1 | 13.5 | 7.7 | 6,757 | 8 | 902 | 5,760 |
| TauriTavern handoff Profiles | 2,000 | later turns | 2/2 | 28.5 | 7.1 | 4,755 | 16 | 2,520 | 9,074 |
| TauriTavern handoff Profiles | 32,000 | turn 1 | 1/1 | 38.0 | 19.8 | 38,788 | 12 | 1,426 | 37,594 |
| TauriTavern handoff Profiles | 32,000 | later turns | 2/2 | 50.1 | 11.4 | 9,511 | 20 | 2,796 | 40,942 |
| TauriTavern single Profile | 2,000 | turn 1 | 1/1 | 8.0 | 4.6 | 5,416 | 5 | 412 | 5,447 |
| TauriTavern single Profile | 2,000 | later turns | 1/2 | 8.9 | 3.7 | 1,452 | 7 | 622 | 6,256 |
| TauriTavern single Profile | 32,000 | turn 1 | 1/1 | 35.0 | 16.1 | 37,321 | 12 | 1,090 | 37,483 |
| TauriTavern single Profile | 32,000 | later turns | 1/2 | 14.5 | 6.1 | 2,852 | 20 | 5,280 | 41,902 |

### Where the time went

Model seconds sum each turn's recorded model calls (recorder ingress to stream end). Non-model seconds are the rest of the wall time: application work, prompt assembly, tool execution, the auditor, chat saving and, for TauriTavern, its WebView bridge and 0.5 s polling.

| Configuration | Median wall s | Median model s | Median non-model s | Median calls |
| --- | ---: | ---: | ---: | ---: |
| Orb | 8.9 | 8.5 | 0.4 | 4 |
| TauriTavern handoff Profiles | 35.2 | 32.0 | 3.5 | 16 |
| TauriTavern single Profile | 11.7 | 9.9 | 1.9 | 7 |

## Repair

Findings come from the shared auditor on the pre-edit draft and the saved reply. Preserved sentences are unflagged draft sentences that survive verbatim.

| Configuration | Qualified turns | Clean drafts | Mean findings, draft → final | Turns with findings left | Unflagged sentences preserved |
| --- | ---: | ---: | --- | ---: | ---: |
| Orb | 6 | 0 | 5.00 → 0.00 | 0 | 121/123 (98.4%) |
| TauriTavern handoff Profiles | 6 | 1 | 3.17 → 0.00 | 0 | 77/77 (100.0%) |
| TauriTavern single Profile | 4 | 1 | 1.00 → 0.00 | 0 | 62/62 (100.0%) |

## Failures and observations

### Native error codes

| Configuration | Error code | Attempts with code |
| --- | --- | ---: |
| Orb | None recorded | 0 |
| TauriTavern handoff Profiles | None recorded | 0 |
| TauriTavern single Profile | `agent.max_tool_rounds_exceeded` | 1 |

### Task-contract failures

| Configuration | Finding | Attempts |
| --- | --- | ---: |
| Orb | None | 0 |
| TauriTavern handoff Profiles | None | 0 |
| TauriTavern single Profile | `editor.whole_file_rewrite` | 1 |
| TauriTavern single Profile | `audit.missing` | 1 |
| TauriTavern single Profile | `native.not_completed` | 1 |
| TauriTavern single Profile | `draft.missing` | 1 |
| TauriTavern single Profile | `completion.audit_commit_finish_order` | 1 |

### Observations (not disqualifying)

| Configuration | Finding | Attempts |
| --- | --- | ---: |
| Orb | `prose.adjacent_quotes.editing` | 2 |
| Orb | `prose.unbalanced_quotes.editing` | 1 |
| TauriTavern handoff Profiles | `save.native_whitespace_cleanup` | 2 |
| TauriTavern handoff Profiles | `editor.edit_batch_limit_exceeded` | 1 |
| TauriTavern handoff Profiles | `response.channel_marker` | 1 |
| TauriTavern single Profile | `save.native_whitespace_cleanup` | 2 |
| TauriTavern single Profile | `response.reasoning_text` | 2 |
| TauriTavern single Profile | `first_prose.no_reply_snapshot` | 1 |

Causes overlap and do not add up to the failure rate. A missing stage after an aborted run is a finding, not a separate attempt.

## Work performed, including failed attempts

| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb | 22 | 4635 | 53624 | unavailable | 10 |
| TauriTavern handoff Profiles | 93 | 12959 | 74077 | 8 | 13 |
| TauriTavern single Profile | 70 | 13306 | 51347 | 32 | 3 |

Orb's turn log does not expose a failed-tool-call denominator, so its column reads `unavailable`. Generated tokens include tool-call JSON and any reasoning-channel output.

| Configuration / stage | Calls | Model seconds | Generated tokens | Uncached input tokens |
| --- | ---: | ---: | ---: | ---: |
| Orb / director | 6 | 22.1 | 824 | 43765 |
| Orb / editor | 10 | 19.6 | 1433 | 8142 |
| Orb / writer | 6 | 22.1 | 2378 | 1717 |
| TauriTavern handoff Profiles / director | 12 | 37.2 | 1696 | 51991 |
| TauriTavern handoff Profiles / editor | 63 | 112.5 | 7959 | 16721 |
| TauriTavern handoff Profiles / writer | 18 | 38.1 | 3304 | 5365 |
| TauriTavern single Profile / single | 70 | 178.1 | 13306 | 51347 |

Recorder forwarding setup: median 0.608 ms, maximum 1.511 ms over 185 calls. Provider cache usage matched llama-server timings in 185 of 185 calls ([cache-crosscheck.json](cache-crosscheck.json)); request-to-stage associations: [association-crosscheck.json](association-crosscheck.json).

## Configuration

Orb `e78029f00af528375b117010607b2fcf0e3f1a45`; TauriTavern 2.3.0 `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375` (release binary SHA-256 `93f9bcc753d821c9be876497635877a54a256697a139321bf246fcfa9885cdb7`); benchmark harness `e78029f00af528375b117010607b2fcf0e3f1a45` with uncommitted changes.

Gemma 4 26B-A4B QAT UD-Q4_K_XL (SHA-256 `a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891`) on llama.cpp (version: 0.5.0-dev (build 1, commit edd6e2bb); built with GNU 13.3.0 for Linux x86_64), RTX 3090 at 270 W, Ubuntu 24.04. Server: 49,152 context, one slot, 4,096 MiB host cache, f16 KV, flash attention, no speculative decoding. Samplers in every request: temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1, max_tokens 4096, cache_prompt, no seed.

Both applications, the request recorder and the MCP auditor ran on the inference host over localhost. TauriTavern ran its release binary in a software-rendered WebKitGTK WebView under Xvfb, driven through its host Agent API with the real prompt-assembly and chat-commit bridges.

TauriTavern Profiles ([tt_setup.js](../../tt_setup.js)): full chat history through a saved preset with stable content before stage instructions; plans, skills, world info and delegation off; 32 rounds and 80 calls per invocation; three model retries. The handoff Director passes its direction as fields of the native handoff brief; the single Profile writes a plain-text direction file. Prompts carry no JSON examples, because Gemma 4 writes tool arguments in its own quoting syntax and imitated JSON quoting trapped earlier runs in an unterminated argument string. The auditor returns the same numbered report and per-category fixing rules that Orb's Editor reads, reworded only where Orb names sentence ids. Orb runs its seeded defaults with the Editor on (`defaults.json`).

Per-attempt data: [turns.csv](turns.csv), [turns.json](turns.json); per-call costs: [calls.csv](calls.csv); grouped medians: [summary.csv](summary.csv).
