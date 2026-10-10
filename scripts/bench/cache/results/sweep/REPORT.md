# Bench 1: Orb and TauriTavern on the same model

360 turns of Orb and two custom TauriTavern agent configurations doing the same job on Gemma 4: write a scene direction, draft a reply from it, audit the draft with Orb's detectors, repair it under Orb's Editor stopping rule, and save it. Starting histories of 2,000, 8,000, 16,000 and 32,000 tokens, 10 scripted consecutive turns per block, 3 repeat blocks per size and arm. llama-server restarts cold before every block and block order rotates across repeats; later turns keep each application's own replies.

**At the 32,000-token start, turns 2–10 take a median 12.6 s in Orb, 35.1 s with TauriTavern handoff Profiles and 20.3 s with a single TauriTavern Profile. Turns meeting the task contract: Orb 115/120, TT handoff 101/120, TT single 95/120.**

There is one frozen history per size, so these are descriptive results for these configurations, not population intervals or a claim about every TauriTavern configuration.

## Figure

![Bench 1: turn time and uncached input against actual context size](figure.svg)

Each small mark is one turn, placed at the largest prompt it sent; large marks and lines are medians per starting history. Wall time uses qualified turns; uncached input and generated tokens use all attempts. The bottom row pools every turn of a size.

- Turn 1 after a cold server start, 32,000 start, median uncached input against median largest prompt: Orb 36,096 of 35,177, TT handoff 38,709 of 38,111, TT single 39,064 of 40,358. Uncached input sums every call in the turn, so it can exceed the largest single prompt.
- Turns 2–10, 32,000 start, the same measure: Orb 3,278 of 37,998, TT handoff 4,874 of 40,566, TT single 2,336 of 39,065.
- Median wall time over turns 2–10 from the 2,000 to the 32,000 start: Orb 9.9 → 12.6 s, TT handoff 24.3 → 35.1 s, TT single 12.8 → 20.3 s.
- Median saved reply (qualified turns) and generated tokens (all attempts): Orb 392 words, 905 tokens; TT handoff 231 words, 2,054 tokens; TT single 213 words, 1,003 tokens.

The arms differ in call count, output length and tool behavior as well as cache reuse; the figure does not separate these causes.

## Completion

Every arm must write a valid scene direction before its draft and use it, audit the draft with the same detectors, re-audit after every edit batch, and save the final reply intact; TauriTavern's trailing-whitespace cleanup counts as intact. A missing moods field, an empty required field, a whole-file rewrite and every native failure fail the turn. An unknown mood id next to valid ones, or a list field given as one delimited string, is an observation: Orb drops unknown ids and renders either shape into Scene Guidance unchanged. Thinking is off on the wire in every request and verified there.

| Configuration | Attempts | Native failures | Met the task contract | Met it by start, 2,000 / 8,000 / 16,000 / 32,000 (of 30 each) |
| --- | ---: | ---: | ---: | ---: |
| Orb | 120 | 0/120 | 115/120 (95.8%) | 30 / 29 / 30 / 26 |
| TauriTavern handoff Profiles | 120 | 13/120 (10.8%) | 101/120 (84.2%) | 28 / 23 / 26 / 24 |
| TauriTavern single Profile | 120 | 15/120 (12.5%) | 95/120 (79.2%) | 27 / 25 / 21 / 22 |

## Latency and cost per turn

Wall time runs from the driver's trigger to the confirmed final save: Orb's `done` after persistence, or TauriTavern's completed Run with its chat settled, less the driver's 0.5 s polling delay (the raw value is `driver_wall_seconds`). First prose is Orb's first Writer token at the client and TauriTavern's first reply text rendered in its chat. Wall and first-prose times are medians over qualified turns; uncached input, calls and tokens are medians over all attempts. Turn 1 follows a cold server start.

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

Model seconds sum the turn's recorded model calls, recorder ingress to stream end. The rest is application work: prompt assembly, tools, the auditor, saving and, for TauriTavern, its WebView bridge.

| Configuration | Median wall s | Median model s | Median non-model s | Median calls |
| --- | ---: | ---: | ---: | ---: |
| Orb | 10.7 | 10.2 | 0.6 | 4 |
| TauriTavern handoff Profiles | 28.3 | 25.9 | 2.7 | 16 |
| TauriTavern single Profile | 16.3 | 14.2 | 2.2 | 11 |

## Repair

Repair is scored where Orb's Editor stopping rule stops: after a re-audit that is clean, leaves no flagged sentence, or did not lower the issue count, and after at most three batches. Orb enforces the rule in code; TauriTavern Profiles are told it, and editing past it stays in their time and calls. Findings are per 1,000 draft words. Preserved sentences are unflagged draft sentences that survive verbatim in the saved reply.

| Configuration | Qualified turns | Clean drafts | Findings per 1,000 words: draft → at stop rule | Turns with findings left | Turns edited past the rule | Unflagged sentences preserved |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| Orb | 115 | 0 | 18.1 → 1.4 | 33 | 0 | 3,418/3,443 (99.3%) |
| TauriTavern handoff Profiles | 101 | 0 | 17.7 → 1.9 | 16 | 16 | 1,378/1,395 (98.8%) |
| TauriTavern single Profile | 95 | 4 | 16.5 → 2.0 | 24 | 24 | 1,210/1,217 (99.4%) |

A mechanical punctuation check runs on every saved reply in every arm and attributes each defect to the Writer's draft or to editing, counting turns.

| Configuration | Defect | From the draft | From editing |
| --- | --- | ---: | ---: |
| Orb | A new sentence after `,"` where the tag should be (`dock," She looks`) | 0/120 | 1/120 |
| TauriTavern single Profile | Unbalanced quotes | 1/120 | 1/120 |
| TauriTavern single Profile | Two quoted lines joined after `."`, `?"` or `!"` | 0/120 | 1/120 |

No other defect appears in any arm. Replaying Orb's Editor rounds with the pinned code reproduces 120 of 120 edited Orb replies byte for byte; its editing defects came from round 2 (`prose.capitalized_after_comma`, 1 turn).

## Failures and observations

Attempts per cause, most frequent first. Causes overlap, so they do not add up to the failure rate; every count is in [findings.json](findings.json).

| Configuration | Native errors | Task-contract failures | Observations (not disqualifying) |
| --- | --- | --- | --- |
| Orb | none | `direction.required.next_event` 3, `direction.required.keywords` 2, `direction.moods` 1 | `editor.findings_remaining` 35, `direction.unknown_moods` 18, `direction.field_shape` 4, `prose.capitalized_after_comma.editing` 1 |
| TauriTavern handoff Profiles | `agent.max_tool_rounds_exceeded` 3, `model.output_truncated` 1 | `completion.commit_then_finish` 13, `native.not_completed` 13, `audit.missing` 6, `editor.whole_file_rewrite` 6, `stages.handoff_sequence` 6, 7 more | `save.native_whitespace_cleanup` 41, `editor.edited_past_stop_rule` 21, `response.reasoning_text` 12, `editor.edit_batch_limit_exceeded` 8, `editor.findings_remaining` 8, 4 more |
| TauriTavern single Profile | `model.output_truncated` 3, `agent.max_tool_rounds_exceeded` 2 | `completion.commit_then_finish` 15, `native.not_completed` 15, `editor.whole_file_rewrite` 10, `audit.final_bytes_mismatch` 8, `audit.missing_before_or_after_edits` 8, 2 more | `editor.edited_past_stop_rule` 31, `response.reasoning_text` 24, `save.native_whitespace_cleanup` 24, `editor.edit_batch_limit_exceeded` 15, `editor.findings_remaining` 15, 5 more |

## Work performed, including failed attempts

| Configuration | Model calls | Generated tokens | Uncached input tokens | Failed tool calls | Edit batches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Orb | 462 | 111,052 | 589,668 | unavailable | 222 |
| TauriTavern handoff Profiles | 2,154 | 291,883 (+1 call without usage) | 812,625 (+1 call without usage) | 511 | 237 |
| TauriTavern single Profile | 1,661 | 183,930 | 561,497 | 302 | 234 |

Orb's turn log has no failed-tool-call count. Generated tokens include tool-call JSON and any reasoning-channel output.

| Configuration / stage | Calls | Model seconds | Generated tokens | Uncached input tokens |
| --- | ---: | ---: | ---: | ---: |
| Orb / director | 120 | 286.2 | 15,740 | 344,037 |
| Orb / editor | 222 | 440.2 | 32,278 | 214,054 |
| Orb / writer | 120 | 557.7 | 63,034 | 31,577 |
| TauriTavern handoff Profiles / director | 357 | 797.7 | 54,553 | 354,965 |
| TauriTavern handoff Profiles / editor | 1,449 | 2,307.5 | 171,368 (+1 call without usage) | 346,303 (+1 call without usage) |
| TauriTavern handoff Profiles / writer | 348 | 727.6 | 65,962 | 111,357 |
| TauriTavern single Profile / single | 1,661 | 2,611.8 | 183,930 | 561,497 |

The recorder added a median 0.541 ms of forwarding setup, 8.690 ms at most, over 4,277 calls. Provider cache usage matched llama-server's timings on 4,276 of 4,276 calls ([cache-crosscheck.json](cache-crosscheck.json)); request-to-stage associations are in [association-crosscheck.json](association-crosscheck.json).

## Disclosures

- Nothing else used the GPU host during the sweep.
- The sweep stopped once between blocks, before any turn of `r1-16000-orb`, on a process-reaping bug in the harness, and resumed from that block. The two harness commits below differ only in process management.

## Configuration

Orb `649bc83ad90982dfb1b26010bbdbf388f7e18ccf`; TauriTavern 2.3.0 `a1855be4a4f8b6ee7cd0374a84dbb3709c3e5375` (release binary SHA-256 `93f9bcc753d821c9be876497635877a54a256697a139321bf246fcfa9885cdb7`); benchmark harness `d216ccc9ebedc4fb92870763682cbf105aa6c83c` (30 blocks) and `fce3729bab3626d1abe69dec665ed5592a7a7de2` (6 blocks).

Gemma 4 26B-A4B QAT UD-Q4_K_XL (SHA-256 `a7c5bc715f5ff8e99a3e8901ce7d2b42b402c669bf24f7c5250747633d0f5891`) on llama.cpp (version: 0.5.0-dev (build 1, commit edd6e2bb); built with GNU 13.3.0 for Linux x86_64), RTX 3090 at 270 W, Ubuntu 24.04. Server: 49,152 context, one slot, 4,096 MiB host cache, f16 KV, flash attention, no speculative decoding. Samplers in every request: temperature 0.8, top_k 40, top_p 0.95, min_p 0, repetition penalty 1, max_tokens 4096, cache_prompt, no seed.

Both applications, the request recorder and the MCP auditor ran on the inference host over localhost. TauriTavern ran its release binary in a software-rendered WebKitGTK WebView under Xvfb, driven through its host Agent API with the real prompt-assembly and chat-commit bridges.

TauriTavern Profiles ([tt_setup.js](../../tt_setup.js)): full chat history through a saved preset with stable content before stage instructions; plans, skills, world info and delegation off; 32 rounds and 80 calls per invocation; three model retries. Both configurations write the direction to a plain-text workspace file (`scratch/direction.md`, one field per line); the handoff Director then hands off to the Writer, which reads that file before drafting, and the Writer hands off to the Editor. Prompts carry no JSON examples, because Gemma 4 writes tool arguments in its own quoting syntax and imitated JSON quoting can leave an argument string unterminated. The auditor returns the same numbered report and per-category fixing rules that Orb's Editor reads, reworded only where Orb names sentence ids. Orb runs its seeded defaults with the Editor on (`defaults.json`).

Every arm gets the same system prompt, card, persona, direction fields and mood descriptions, Orb's Director brief, the same Scene Guidance reading of the direction, the same audit report and fixing rules, and the same Editor stopping rule. Native differences kept on purpose: Orb's Director sees the previously active moods and Orb releases an ended mood with its negative prompt, while each TauriTavern turn starts from an empty workspace with no mood state; Orb's first prose is timed at the client before rendering, TauriTavern's when its WebView renders it; neither arm is told a reply length.

Per-attempt data: [turns.csv.gz](turns.csv.gz), [turns.json.gz](turns.json.gz); per-call costs: [calls.csv.gz](calls.csv.gz); grouped medians: [summary.csv](summary.csv); figure: [figure.svg](figure.svg).
