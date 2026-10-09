# Bench 1 comparison sweep

360 attempted turns across Orb and two custom TauriTavern configurations; 311 met the shared task contract. Starting histories: 2,000, 8,000, 16,000, 32,000 nominal tokens; 10 consecutive scripted user turns per block; 3 repeat block(s) per size and arm. llama-server restarted cold before every block, and block order rotated across repeats. Starting histories were shared; later turns kept each application's own replies, including the effect of failed turns.

There is one frozen history per size, so these are descriptive results for this fixture and these configurations, not population intervals and not a claim about every TauriTavern configuration.

## Task contract

Every arm must produce a valid scene direction before its draft and use it, audit the draft with the same detectors, re-audit after every edit batch, and save the final reply intact. TauriTavern's native save cleanup (trailing whitespace) counts as intact. How editing ended (findings left, more than three batches), reasoning-channel output and save cleanup are reported below as observations for every arm; they do not disqualify a turn. Thinking is disabled on the wire in every request and verified there.

## Completion

| Configuration / starting history | Attempts | Native failures | Qualified |
| --- | ---: | ---: | ---: |
| Orb / all | 120 | 0/120 (0.0%) | 115/120 (95.8%) |
| Orb / 2,000 | 30 | 0/30 (0.0%) | 30/30 (100.0%) |
| Orb / 8,000 | 30 | 0/30 (0.0%) | 29/30 (96.7%) |
| Orb / 16,000 | 30 | 0/30 (0.0%) | 30/30 (100.0%) |
| Orb / 32,000 | 30 | 0/30 (0.0%) | 26/30 (86.7%) |
| TauriTavern handoff Profiles / all | 120 | 13/120 (10.8%) | 101/120 (84.2%) |
| TauriTavern handoff Profiles / 2,000 | 30 | 2/30 (6.7%) | 28/30 (93.3%) |
| TauriTavern handoff Profiles / 8,000 | 30 | 3/30 (10.0%) | 23/30 (76.7%) |
| TauriTavern handoff Profiles / 16,000 | 30 | 3/30 (10.0%) | 26/30 (86.7%) |
| TauriTavern handoff Profiles / 32,000 | 30 | 5/30 (16.7%) | 24/30 (80.0%) |
| TauriTavern single Profile / all | 120 | 15/120 (12.5%) | 95/120 (79.2%) |
| TauriTavern single Profile / 2,000 | 30 | 1/30 (3.3%) | 27/30 (90.0%) |
| TauriTavern single Profile / 8,000 | 30 | 1/30 (3.3%) | 25/30 (83.3%) |
| TauriTavern single Profile / 16,000 | 30 | 7/30 (23.3%) | 21/30 (70.0%) |
| TauriTavern single Profile / 32,000 | 30 | 6/30 (20.0%) | 22/30 (73.3%) |

## Figure

![Bench 1: turn time and uncached input against actual context size](figure.svg)

Each small mark is one turn, placed at the largest prompt it sent; large marks and lines are medians per starting history. Wall time uses qualified turns; uncached input and generated tokens use all attempts. The bottom row pools every turn of a size.

- Turn 1 after a cold server start, 32,000 start, median uncached input against median largest prompt: Orb 36,096 of 35,177, TT handoff 38,709 of 38,111, TT single 39,064 of 40,358. Uncached input sums every call in the turn, so it can exceed the largest single prompt.
- Turns 2–10, 32,000 start, the same measure: Orb 3,278 of 37,998, TT handoff 4,874 of 40,566, TT single 2,336 of 39,065.
- Median wall time over turns 2–10 from the 2,000 to the 32,000 start: Orb 9.9 → 12.6 s, TT handoff 24.3 → 35.1 s, TT single 12.8 → 20.3 s.
- Turns meeting the task contract: Orb 115/120, TT handoff 101/120, TT single 95/120.
- Median saved reply (qualified turns) and generated tokens (all attempts): Orb 392 words, 905 tokens; TT handoff 231 words, 2,054 tokens; TT single 213 words, 1,003 tokens.

The arms differ in call count, output length and tool behavior as well as cache reuse; the figure does not separate these causes.

## Latency and cost per turn

Wall time runs from the driver's trigger to Orb's `done` after persistence, or to TauriTavern's completed Run with its chat presentation settled. Orb's end is pushed over SSE; TauriTavern's is found by the driver polling every 0.5 s, so the delay between TauriTavern's persisted terminal time and the driver noticing it is removed from its wall time (the raw driver time is `driver_wall_seconds` in turns.json). Medians over qualified turns; turn 1 follows a cold server start and is reported apart from turns 2–10. First visible prose is Orb's first streamed Writer token at the client, and TauriTavern's first reply text rendered in its chat message (checked against the start of the reply). Uncached input, calls and generated tokens are medians over all attempts, failures included.

| Configuration | Start | Turns | Qualified | Wall s | First prose s | Uncached input | Model calls | Generated tokens | Prompt tokens, max |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Orb | 2,000 | turn 1 | 3/3 | 9.6 | 3.2 | 5,875 | 4 | 933 | 4,536 |
| Orb | 2,000 | later turns | 27/27 | 9.9 | 2.4 | 3,335 | 4 | 992 | 7,448 |
| Orb | 8,000 | turn 1 | 3/3 | 10.9 | 4.8 | 11,922 | 4 | 786 | 10,526 |
| Orb | 8,000 | later turns | 26/27 | 9.4 | 2.4 | 3,248 | 4 | 860 | 13,105 |
| Orb | 16,000 | turn 1 | 3/3 | 13.8 | 7.5 | 19,588 | 3 | 773 | 18,700 |
| Orb | 16,000 | later turns | 27/27 | 11.0 | 2.8 | 3,422 | 4 | 901 | 21,381 |
| Orb | 32,000 | turn 1 | 3/3 | 18.7 | 13.4 | 36,096 | 3 | 672 | 35,177 |
| Orb | 32,000 | later turns | 23/27 | 12.6 | 3.8 | 3,278 | 4 | 898 | 37,998 |
| TauriTavern handoff Profiles | 2,000 | turn 1 | 3/3 | 26.3 | 8.3 | 8,495 | 16 | 2,120 | 7,769 |
| TauriTavern handoff Profiles | 2,000 | later turns | 25/27 | 24.3 | 7.8 | 4,401 | 15 | 1,983 | 9,630 |
| TauriTavern handoff Profiles | 8,000 | turn 1 | 2/3 | 28.3 | 10.5 | 16,129 | 16 | 2,532 | 14,784 |
| TauriTavern handoff Profiles | 8,000 | later turns | 21/27 | 28.8 | 8.1 | 4,952 | 17 | 2,489 | 16,365 |
| TauriTavern handoff Profiles | 16,000 | turn 1 | 3/3 | 25.8 | 12.7 | 22,390 | 13 | 1,427 | 21,089 |
| TauriTavern handoff Profiles | 16,000 | later turns | 23/27 | 27.3 | 8.5 | 4,450 | 16 | 2,013 | 23,859 |
| TauriTavern handoff Profiles | 32,000 | turn 1 | 2/3 | 39.2 | 20.4 | 38,709 | 16 | 1,377 | 38,111 |
| TauriTavern handoff Profiles | 32,000 | later turns | 22/27 | 35.1 | 11.2 | 4,874 | 15 | 2,047 | 40,566 |
| TauriTavern single Profile | 2,000 | turn 1 | 2/3 | 15.6 | 4.7 | 6,430 | 13 | 1,011 | 6,842 |
| TauriTavern single Profile | 2,000 | later turns | 25/27 | 12.8 | 4.3 | 2,007 | 11 | 837 | 8,284 |
| TauriTavern single Profile | 8,000 | turn 1 | 3/3 | 12.8 | 6.3 | 11,976 | 8 | 575 | 12,087 |
| TauriTavern single Profile | 8,000 | later turns | 22/27 | 17.8 | 4.3 | 2,349 | 13 | 1,025 | 14,523 |
| TauriTavern single Profile | 16,000 | turn 1 | 3/3 | 16.4 | 9.0 | 20,349 | 7 | 675 | 20,348 |
| TauriTavern single Profile | 16,000 | later turns | 18/27 | 17.5 | 4.8 | 3,111 | 16 | 1,202 | 23,500 |
| TauriTavern single Profile | 32,000 | turn 1 | 2/3 | 46.2 | 16.3 | 39,064 | 25 | 2,241 | 40,358 |
| TauriTavern single Profile | 32,000 | later turns | 20/27 | 20.3 | 5.9 | 2,336 | 10 | 1,099 | 39,065 |

### Where the time went

Model seconds sum each turn's recorded model calls (recorder ingress to stream end). Non-model seconds are the rest of the wall time: application work, prompt assembly, tool execution, the auditor, chat saving and, for TauriTavern, its WebView bridge and 0.5 s polling.

| Configuration | Median wall s | Median model s | Median non-model s | Median calls |
| --- | ---: | ---: | ---: | ---: |
| Orb | 10.7 | 10.2 | 0.6 | 4 |
| TauriTavern handoff Profiles | 28.3 | 25.9 | 2.7 | 16 |
| TauriTavern single Profile | 16.3 | 14.2 | 2.2 | 11 |

## Repair

Every arm follows Orb's Editor stopping rule: after each re-audit, stop when it is clean, when no flagged sentence is left, or when the issue count did not go down; at most three batches. Orb enforces the rule in code; TauriTavern Profiles are told it. Repair is scored where the rule stops, from the shared auditor's recorded audits; TauriTavern editing past that point is counted below and stays in its time and calls. Findings are per 1,000 words, because replies differ in length. Preserved sentences are unflagged draft sentences that survive verbatim in the saved reply.

| Configuration | Qualified turns | Clean drafts | Findings per 1,000 words: draft → at stop rule | Turns with findings left at stop rule | Turns edited past the rule | Unflagged sentences preserved |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Orb | 115 | 0 | 18.1 → 1.4 | 33 | 0 | 3418/3443 (99.3%) |
| TauriTavern handoff Profiles | 101 | 0 | 17.7 → 1.9 | 16 | 16 | 1378/1395 (98.8%) |
| TauriTavern single Profile | 95 | 4 | 16.5 → 2.0 | 24 | 24 | 1210/1217 (99.4%) |

## Punctuation damage in saved replies

A mechanical check on every saved reply, the same for every arm, attributed to its mechanism: already in the Writer's draft (TauriTavern drafts prose inside a tool argument), or introduced by editing. Turns counted, not occurrences.

| Configuration | Defect | From the draft | From editing |
| --- | --- | ---: | ---: |
| Orb | Unbalanced quotes | 0/120 | 0/120 |
| Orb | Two quoted lines together after `,"` or a dash (always a defect) | 0/120 | 0/120 |
| Orb | A new sentence after `,"` where the tag should be (`dock," She looks`) | 0/120 | 1/120 |
| Orb | Two quoted lines together after `."`, `?"` or `!"` (new speech is allowed; needs a look) | 0/120 | 0/120 |
| TauriTavern handoff Profiles | Unbalanced quotes | 0/120 | 0/120 |
| TauriTavern handoff Profiles | Two quoted lines together after `,"` or a dash (always a defect) | 0/120 | 0/120 |
| TauriTavern handoff Profiles | A new sentence after `,"` where the tag should be (`dock," She looks`) | 0/120 | 0/120 |
| TauriTavern handoff Profiles | Two quoted lines together after `."`, `?"` or `!"` (new speech is allowed; needs a look) | 0/120 | 0/120 |
| TauriTavern single Profile | Unbalanced quotes | 1/120 | 1/120 |
| TauriTavern single Profile | Two quoted lines together after `,"` or a dash (always a defect) | 0/120 | 0/120 |
| TauriTavern single Profile | A new sentence after `,"` where the tag should be (`dock," She looks`) | 0/120 | 0/120 |
| TauriTavern single Profile | Two quoted lines together after `."`, `?"` or `!"` (new speech is allowed; needs a look) | 0/120 | 1/120 |

1 turn(s) have only an after-stop hit and need reading: r1-2000-tt-single turn 2.

Orb's Editor rounds were replayed with the pinned code (re-audit, rebuild the numbered targets, apply each `editor_apply_patch` call); 120 of 120 edited Orb turns reproduce the saved reply byte for byte. Defects by the round that introduced them:

| Editor round | Defect | Orb turns |
| ---: | --- | ---: |
| 2 | `prose.capitalized_after_comma` | 1 |

## Disclosures

### Sweep — harness fce3729b, Orb 649bc83a, TauriTavern a1855be4

Reportable run: all three arms, history 2k/8k/16k/32k, 10 turns per block, 3 repeats (360 turns).

Known conditions:

- Nothing else is scheduled on the GPU host. Any interference during the run is added below.
- **Interrupted once, resumed.** After `r1-8000-tt-single`, setting up `r1-16000-orb` stopped the sweep before any turn: the previous Orb server had exited but stayed unreaped, and the restart guard read its empty command line as a foreign process. The block was set aside (`r1-16000-orb.setup-failed-*`, no turns) and the same command resumed from it at harness `d216ccc9`, which only reaps such a process and, on resume, re-adds the set-aside block's Orb worktree with `--force` (a first resume attempt at `4c517fb4` failed on that before any turn: `r1-16000-orb.setup-failed-1791544105478243126`) (process management; no measured code changed — `git diff fce3729b d216ccc9` touches `stop()`, the Orb worktree add and report wording). The recorder and auditor were restarted from that harness before resuming. Blocks before the interruption record harness `fce3729b`, later ones `d216ccc9`.
- During the first blocks, another session read a few small turn files on the host (`ssh`, `nice -n 19` Python over `tool-args`/`tool-results`, CPU only) to check two TauriTavern failures. Nothing touched the GPU.
- **Scoring rules settled after the first scoring pass, the same for every arm.** The first pass (scorer `d216ccc9`) failed 15 Orb turns for an invented mood id next to valid ones, 3 for `keywords` given as one string, and 1 TauriTavern turn for an invented mood id. Orb drops unknown mood ids and renders either field shape into Scene Guidance unchanged, and the TauriTavern direction file already had delimited lists accepted, so these are now observations (`direction.unknown_moods`, `direction.field_shape`). Reading the single-cause TauriTavern failures the same way, an audit of the unchanged text after the commit and a handoff Writer's early commit are observations (`completion.audited_after_commit`, `stages.writer_commit`); the saved bytes must still match an audit and commit then finish must close the run. A missing moods field, a missing or empty required field, a whole-file rewrite and every native failure still fail; an empty required list now fails like an empty required string (2 Orb turns with `keywords: []`, which Orb does not pass to the Writer; the first pass let them through).

## Failures and observations

### Native error codes

| Configuration | Error code | Attempts with code |
| --- | --- | ---: |
| Orb | None recorded | 0 |
| TauriTavern handoff Profiles | `agent.max_tool_rounds_exceeded` | 3 |
| TauriTavern handoff Profiles | `model.output_truncated` | 1 |
| TauriTavern single Profile | `model.output_truncated` | 3 |
| TauriTavern single Profile | `agent.max_tool_rounds_exceeded` | 2 |

### Task-contract failures

| Configuration | Finding | Attempts |
| --- | --- | ---: |
| Orb | `direction.required.next_event` | 3 |
| Orb | `direction.required.keywords` | 2 |
| Orb | `direction.moods` | 1 |
| TauriTavern handoff Profiles | `native.not_completed` | 13 |
| TauriTavern handoff Profiles | `completion.commit_then_finish` | 13 |
| TauriTavern handoff Profiles | `stages.handoff_sequence` | 6 |
| TauriTavern handoff Profiles | `audit.missing` | 6 |
| TauriTavern handoff Profiles | `stages.invocation_sequence` | 6 |
| TauriTavern handoff Profiles | `editor.whole_file_rewrite` | 6 |
| TauriTavern handoff Profiles | `draft.missing` | 4 |
| TauriTavern handoff Profiles | `audit.missing_before_or_after_edits` | 4 |
| TauriTavern handoff Profiles | `audit.final_bytes_mismatch` | 4 |
| TauriTavern handoff Profiles | `stages.unexpected_action` | 2 |
| TauriTavern handoff Profiles | `direction.not_consumed_before_draft` | 2 |
| TauriTavern handoff Profiles | `response.incomplete` | 1 |
| TauriTavern single Profile | `native.not_completed` | 15 |
| TauriTavern single Profile | `completion.commit_then_finish` | 15 |
| TauriTavern single Profile | `editor.whole_file_rewrite` | 10 |
| TauriTavern single Profile | `audit.missing_before_or_after_edits` | 8 |
| TauriTavern single Profile | `audit.final_bytes_mismatch` | 8 |
| TauriTavern single Profile | `audit.missing` | 2 |
| TauriTavern single Profile | `draft.missing` | 1 |

### Observations (not disqualifying)

| Configuration | Finding | Attempts |
| --- | --- | ---: |
| Orb | `editor.findings_remaining` | 35 |
| Orb | `direction.unknown_moods` | 18 |
| Orb | `direction.field_shape` | 4 |
| Orb | `prose.capitalized_after_comma.editing` | 1 |
| TauriTavern handoff Profiles | `save.native_whitespace_cleanup` | 41 |
| TauriTavern handoff Profiles | `editor.edited_past_stop_rule` | 21 |
| TauriTavern handoff Profiles | `response.reasoning_text` | 12 |
| TauriTavern handoff Profiles | `editor.findings_remaining` | 8 |
| TauriTavern handoff Profiles | `editor.edit_batch_limit_exceeded` | 8 |
| TauriTavern handoff Profiles | `first_prose.no_reply_snapshot` | 4 |
| TauriTavern handoff Profiles | `stages.writer_commit` | 2 |
| TauriTavern handoff Profiles | `completion.audited_after_commit` | 2 |
| TauriTavern handoff Profiles | `direction.unknown_moods` | 1 |
| TauriTavern single Profile | `editor.edited_past_stop_rule` | 31 |
| TauriTavern single Profile | `response.reasoning_text` | 24 |
| TauriTavern single Profile | `save.native_whitespace_cleanup` | 24 |
| TauriTavern single Profile | `editor.findings_remaining` | 15 |
| TauriTavern single Profile | `editor.edit_batch_limit_exceeded` | 15 |
| TauriTavern single Profile | `prose.adjacent_quotes_after_stop.editing` | 1 |
| TauriTavern single Profile | `first_prose.no_reply_snapshot` | 1 |
| TauriTavern single Profile | `response.channel_marker` | 1 |
| TauriTavern single Profile | `prose.unbalanced_quotes.draft` | 1 |
| TauriTavern single Profile | `prose.unbalanced_quotes.editing` | 1 |

Causes overlap and do not add up to the failure rate. A missing stage after an aborted run is a finding, not a separate attempt.

## Work performed, including failed attempts

| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb | 462 | 111052 | 589668 | unavailable | 222 |
| TauriTavern handoff Profiles | 2154 | 291883 (+1 call without usage) | 812625 (+1 call without usage) | 511 | 237 |
| TauriTavern single Profile | 1661 | 183930 | 561497 | 302 | 234 |

Orb's turn log does not expose a failed-tool-call denominator, so its column reads `unavailable`. Generated tokens include tool-call JSON and any reasoning-channel output.

| Configuration / stage | Calls | Model seconds | Generated tokens | Uncached input tokens |
| --- | ---: | ---: | ---: | ---: |
| Orb / director | 120 | 286.2 | 15740 | 344037 |
| Orb / editor | 222 | 440.2 | 32278 | 214054 |
| Orb / writer | 120 | 557.7 | 63034 | 31577 |
| TauriTavern handoff Profiles / director | 357 | 797.7 | 54553 | 354965 |
| TauriTavern handoff Profiles / editor | 1449 | 2307.5 | 171368 (+1 call without usage) | 346303 (+1 call without usage) |
| TauriTavern handoff Profiles / writer | 348 | 727.6 | 65962 | 111357 |
| TauriTavern single Profile / single | 1661 | 2611.8 | 183930 | 561497 |

Recorder forwarding setup: median 0.541 ms, maximum 8.690 ms over 4277 calls. Provider cache usage matched llama-server timings in 4276 of 4276 calls ([cache-crosscheck.json](cache-crosscheck.json)); request-to-stage associations: [association-crosscheck.json](association-crosscheck.json).

## Configuration

Orb `649bc83ad90982dfb1b26010bbdbf388f7e18ccf`; TauriTavern 2.3.0 `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375` (release binary SHA-256 `93f9bcc753d821c9be876497635877a54a256697a139321bf246fcfa9885cdb7`); benchmark harness `d216ccc9ebedc4fb92870763682cbf105aa6c83c`; scored and reported by `3252a2c072e9ab4cf8698dc7236de06da6597f1c`.

Gemma 4 26B-A4B QAT UD-Q4_K_XL (SHA-256 `a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891`) on llama.cpp (version: 0.5.0-dev (build 1, commit edd6e2bb); built with GNU 13.3.0 for Linux x86_64), RTX 3090 at 270 W, Ubuntu 24.04. Server: 49,152 context, one slot, 4,096 MiB host cache, f16 KV, flash attention, no speculative decoding. Samplers in every request: temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1, max_tokens 4096, cache_prompt, no seed.

Both applications, the request recorder and the MCP auditor ran on the inference host over localhost. TauriTavern ran its release binary in a software-rendered WebKitGTK WebView under Xvfb, driven through its host Agent API with the real prompt-assembly and chat-commit bridges.

TauriTavern Profiles ([tt_setup.js](../../tt_setup.js)): full chat history through a saved preset with stable content before stage instructions; plans, skills, world info and delegation off; 32 rounds and 80 calls per invocation; three model retries. Both configurations write the direction to a plain-text workspace file (`scratch/direction.md`, one field per line); the handoff Director then hands off to the Writer, which reads that file before drafting, and the Writer hands off to the Editor. Prompts carry no JSON examples, because Gemma 4 writes tool arguments in its own quoting syntax and imitated JSON quoting trapped earlier runs in an unterminated argument string. The auditor returns the same numbered report and per-category fixing rules that Orb's Editor reads, reworded only where Orb names sentence ids. Orb runs its seeded defaults with the Editor on (`defaults.json`).

Every arm gets the same system prompt, card, persona, direction fields and mood descriptions, Orb's Director brief, the same Scene Guidance reading of the direction, the same audit report and fixing rules, and the same Editor stopping rule. Native differences kept on purpose: Orb's Director sees the previously active moods and Orb releases an ended mood with its negative prompt, while each TauriTavern turn starts from an empty workspace with no mood state; Orb's first prose is timed at the client before rendering, TauriTavern's when its WebView renders it; neither arm is told a reply length.

Per-attempt data: [turns.csv](turns.csv), [turns.json](turns.json); per-call costs: [calls.csv](calls.csv); grouped medians: [summary.csv](summary.csv); figure: [figure.svg](figure.svg).
