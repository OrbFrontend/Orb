# Bench 2 Gemma pilot

5 contexts × Director on/off × 1 repeat = 10 attempted turns on Gemma 4 26B-A4B (`UD-Q4_K_XL`, sha256 `a7c5bc71…`), llama.cpp `edd6e2bb`, Orb `649bc83a`, both arms on [bench.json](../../bench.json) with `enabled_tools.direct_scene` on or off and `enable_agent` = 1. One Orb worktree and one cold llama-server for the run; arm order alternates by context. Harness: uncommitted on `743b60a5`. Corpus: the first opening of each card, sha256 `8aac99b6…` as applied.

A pilot: five contexts, one repeat. The numbers size the full run; they are not a README result.

## Completion

| Arm | Attempted | Clean | Wire checks failed | Reasoning chars |
| --- | ---: | ---: | ---: | ---: |
| Director on | 5 | 5 | 0 | 0 |
| Director off | 5 | 5 | 0 | 0 |

Every turn ran lorebook selection, Writer, output auditor/Editor and state; the Director ran in exactly the five "on" turns. The saved reply equals `writer_draft` in all 10, and in the two turns the Editor left alone the streamed draft equals the saved reply byte for byte. The subject analyzer tagged every reply; `subject_fixation` flagged nothing. Turns took 4.5–10.6 s at this history length.

## Jev `shape` labels

Scored with the current wording in [jev_check.py](../../jev_check.py); live preflight 12/12, returned model `typesafe/jev-1.13-20260917`. Raw answers under both wordings: [jev-raw.jsonl](jev-raw.jsonl).

| Arm | Driven (P ≥ 0.75) | Jev label: driven / afterthought / static | Mean P(driven) | Mean words |
| --- | ---: | ---: | ---: | ---: |
| Director on | 3/5 | 4 / 1 / 0 | 0.67 | 293 |
| Director off | 0/5 | 1 / 1 / 3 | 0.29 | 241 |

Paired difference in `driven` share, on minus off: +0.60 at the threshold and +0.60 by Jev's label, 95% interval 0.20 to 1.00 either way. Director-on replies are 21% longer; within pooled length tertiles, on is driven in 1/1, 0/1 and 3/3 and off in 1/2, 0/2 and 0/1, too few to separate length from the arm. Per turn: [turns.csv](turns.csv).

## The judge

- **The first wording over-called `driven`.** On four dialogue probes (three where a character asks questions and nothing changes, one real pivot), it labeled two question-only replies `driven` at 0.68 and 0.59, as it did two pilot replies. The current wording labels all four correctly and keeps the original 8 fixtures; the probes are now preflight fixtures.
- **Hand labels** ([sheet](hand-labels.md), [key](hand-labels-key.json), [comparison](hand-labels-result.json)), blind: 3/5 driven on, 0/5 off. Jev's own label agreed on driven-or-not in 8 of 10, with `driven` precision 0.6 and recall 1.0: it also called `odile-off` (0.59, hand: static) and `ines-on` (0.69, hand: afterthought) driven. The hand-labeled driven replies scored 0.82, 0.83 and 0.97, and every other reply 0.69 or less, so `driven` is now P(driven) ≥ 0.75, pre-registered for the full run. It matches all 10 here by construction; the full run's hand labels test it. Afterthought versus static carries little signal: the hand labels call nearly every non-driven reply static.
- **Noise.** Asked three times, 2 of 10 replies changed label (one across the `driven` line: `tomas-off`, 0.38–0.51), and P(driven) moved by 0.05 on average, 0.13 at most. One reply crossed the threshold on a re-ask (`ines-on`, 0.65–0.76).

## What the text shows

- **The Director's event usually arrives, sometimes last.** In `tomas-on` the Director asked for a guest to approach; the Writer put him in the final paragraph, which reads as an afterthought.
- **Editor opener fixes damage grammar.** "She grabbed a small white plate" became "A small white plate was grabbed from the stack"; "She began reading from the list" became "Reading from the list…, the words flowed", a dangling modifier. Bench 3 counts both as repairs.
- **Detectors flag scene nouns and dialogue tags.** `phrase_repetition` flagged "the pie case", which the user had just typed, and the Editor renamed it "the glass display"; "the peach seller" became "the man selling fruit". `repetitive_openers` flagged a dialogue tag split from its quote ("she muttered, turning back…"); the Editor declined to change it and the no-op patch was rejected.
- **State re-adds the starting inventory.** In 8 of 10 turns the state step re-proposed both seeded items and Orb rejected them as duplicates. The 5 accepted adds are soup, bread, tea, a book and a pressed flower: grounded, but not items that matter to the story.
- **Lorebook picks are sensible** in every turn, e.g. all four Ostrander's Patience entries for the departure scene.

## DeepSeek smoke

2 turns of `ines-1` on `deepseek/deepseek-v4.1-flash` through the pinned upstream: both clean, every call answered by Sail Research with 0 reasoning, $0.001 in total. The prose is markedly better than Gemma's and draws on the lorebook. The Director-off reply already brings in an interruption mid-reply, so DeepSeek's Director effect may be smaller. Its state step stored "a roof repair quote of eleven hundred dollars" as an inventory item.
