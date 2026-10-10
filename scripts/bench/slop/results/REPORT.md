# Bench 3: Slop before and after the Editor

Does the Editor remove what Orb's auditor flags, without adding new findings or rewriting the prose around them?

**The Editor fixes 97% of flagged slop on both models (Gemma 289/297, DeepSeek 257/266) and leaves 97% of unflagged
sentences byte-identical. On Gemma it also cuts held-out slop, a phrase list the Editor never sees, by 20% (66 → 52
hits). DeepSeek's drafts have almost no held-out slop to cut (8 → 9 hits in 32,500 words).**

## Setup

- **Turns.** All 240 of Bench 2's turns: 60 contexts × Director on/off × Gemma 4 and DeepSeek V4.1 Flash
  ([Bench 2 report](../../driven/results/REPORT.md)). No new generation. Every turn is clean, and the scorer rejects
  any turn with reasoning output, so every draft and reply here was written with thinking off.
- **Before.** The Writer's draft: the `token` events before `writer_done`. Its length matches the Editor's logged
  input on all 240 turns.
- **After.** The saved reply, after the Editor's patch loop and the subject fixation edit. `messages.content`
  equals `messages.writer_draft` on every turn.
- **Detectors.** Every detector in [bench.json](../../driven/bench.json), run by [score.py](../score.py) through
  `backend.analysis` at Orb `8821c071` (the scorer's checkout has the same `backend/`), with the run's own phrase bank
  and the same inputs the Editor had: the two earlier replies and the user's message. Draft-side counts match the
  Editor's logged initial audit on 235 of 240 turns, and its target count on all 240. On the other 5, the scorer counts
  each phrase-bank hit while the log counts each flagged sentence.
- **Finding identity.** A finding is repaired when its key (the phrase, opener, template, sentence or span) is gone
  from the reply, and introduced when the reply has a key the draft lacked. `banned_phrases` uses the seeded 39-group
  bank.
- **Held-out list.** antislop-sampler `slop_phrases_2025-04-07.json` at `6aa25403` (Apache-2.0, sha256 `d0bc80be…`).
  It has 2,408 distinct phrases after lowercasing; 209 that the seeded bank's own matcher flags are dropped, leaving
  2,199. Hits are whole-word, case-insensitive phrase matches. Overlapping entries each count ("casting long, dancing"
  and "long, dancing shadows").
- **Intervals.** 95% bootstrap over the 60 contexts, with both arms of a context resampled together.

## Results

| Model | Repair, 7 audit types | Findings per 1k words | Introduced per 1k | Held-out per 1k (hits) | Unflagged sentences kept | Words |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Gemma | 97.3% (95.3–99.0), 289/297 | 8.74 → 0.36 | 0.12 (0.00–0.30), 4 | 1.94 → 1.56 (66 → 52), −20% (−7 to −33) | 97.1% (95.1–98.7), 1,020/1,050 | 33,980 → 33,398 (−1.7%) |
| DeepSeek | 96.6% (94.2–98.7), 257/266 | 8.18 → 0.43 | 0.15 (0.03–0.32), 5 | 0.25 → 0.28 (8 → 9) | 96.9% (95.2–98.4), 976/1,007 | 32,510 → 32,473 (−0.1%) |

By detector (draft findings → findings left in the reply; repaired, introduced):

| Detector | Gemma | DeepSeek |
| --- | ---: | ---: |
| `banned_phrases` (39-group seeded bank) | 112 → 5 (107 repaired, 0 introduced) | 5 → 0 (5, 0) |
| `repetitive_openers` | 40 → 1 (39, 0) | 43 → 3 (40, 0) |
| `repetitive_templates` | 7 → 1 (7, 1) | 36 → 0 (36, 0) |
| `contrastive_negation` | 48 → 3 (47, 2) | 16 → 2 (14, 0) |
| `phrase_repetition` | 90 → 2 (89, 1) | 166 → 9 (162, 5) |
| `structural_repetition` | 0 | 0 |
| `anti_echo` | 0 | 0 |
| `negated_narration`, apart | 9 → 4 (7, 2) | 31 → 0 (31, 0) |
| `subject_fixation`, apart | 17 → 10 (7, 0), 41% | 16 → 9 (9, 2), 56% |

By Bench 2 arm (7 audit types):

| Model | Arm | Repair | Introduced | Held-out hits | Unflagged kept |
| --- | --- | ---: | ---: | ---: | ---: |
| Gemma | Director on | 157/160 (98.1%) | 2 | 39 → 28 | 98.1% |
| Gemma | Director off | 132/137 (96.4%) | 2 | 27 → 24 | 96.0% |
| DeepSeek | Director on | 137/142 (96.5%) | 1 | 4 → 4 | 96.7% |
| DeepSeek | Director off | 120/124 (96.8%) | 4 | 4 → 5 | 97.1% |

- **Repairs are mostly rewrites.** Of the flagged sentences the Editor changed, 12.6% (Gemma, 49 of 389) and
  15.0% (DeepSeek, 47 of 314) are gone from the reply with no reply sentence at least half as similar; the rest were
  rewritten. 20 and 34 flagged sentences were left verbatim. Deleting a sentence counts as a repair here, and Gemma's
  replies lose 1.7% of their words.
- **Introduced findings come from the Editor's own wording.** One Gemma patch replaced flagged sentences with "Though
  she didn't look up, she seemed aware of your presence…", which `contrastive_negation` flags; DeepSeek's are repeated
  phrases ("the toe of his boot") that its patches carried in from the earlier replies.
- **Held-out slop is a Gemma measure.** 47 of Gemma's 120 drafts have a held-out hit, against 8 of DeepSeek's. The
  phrases the Editor leaves are ones it was never pointed at ("long, dancing shadows", "felt less like", "her
  expression unreadable"). DeepSeek's change (+1 hit) is within noise.
- **The subject fixation edit fixes about half of what it flags.** Gemma 7 of 17, DeepSeek 9 of 16. It flags far less
  than the audit types and is reported apart, as is `negated_narration` (Gemma 7 of 9, DeepSeek 31 of 31).

## Limits

- **Short history.** Each turn has two earlier replies. `structural_repetition` and `anti_echo` never fired, and the
  subject fixation presence rule (4 of the last 4 replies) cannot fire, so only its pair-confirmed nominees are counted.
  Repetition detectors would see more in long chats.
- **The corpus is Bench 2's.** All turns run `bench.json` (every detector on), not the out-of-the-box Editor settings,
  and every user turn is an open, passive one.
- **The detectors define the target.** This bench measures removal of what the auditor flags, not whether each flag is
  slop; edit damage seen while reading output is not scored.

## Files

- [summary.json](summary.json): every measure, pooled and by arm, with the held-out entries dropped.
- [gemma-turns.json](gemma-turns.json), [deepseek-turns.json](deepseek-turns.json): per-turn counts.
- Rebuild: `PYTHONPATH=. .venv/bin/python -m scripts.bench.slop.score --run RUN --run RUN --heldout PHRASES --output OUT`
  on Bench 2's run folders, with the held-out file fetched from the URL in [score.py](../score.py).
