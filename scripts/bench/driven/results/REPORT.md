# Bench 2: Driven replies

Does turning the Director on raise the share of replies where the situation changes partway through and the reply
develops it, rather than describing, chatting or tacking a hook onto the end?

**On open-ended user turns, 37% of Gemma 4 replies are driven with the Director on, 18% with it off (+0.18, 95%
interval +0.03 to +0.33). DeepSeek V4.1 Flash is a boring Director: 38% on, 30% off (+0.08, −0.05 to +0.22).**

## Setup

- **Corpus.** 60 safe-for-work cards, one opening each, every opening ending on an open, passive user turn ("I take
  a seat by the stove and watch her work"). Cards 1–20 are hand-written ([corpus.py](../corpus.py),
  [more_cards.py](../more_cards.py)). Cards 21–60 were drafted by the pinned DeepSeek from one-line premises with
  nothing already on its way, and reviewed by hand ([draft_cards.py](../draft_cards.py),
  [drafted_cards.json](../drafted_cards.json)). Corpus sha256 `d1c68fb2…`.
- **Arms.** [bench.json](../bench.json) in both, with `enabled_tools.direct_scene` on or off; `enable_agent` = 1, so
  lorebook selection, the Editor and the state step run in both. Arm order alternates by context. 60 contexts × 2 arms
  × 1 repeat = 120 turns per model.
- **Models.** Gemma 4 26B-A4B (`UD-Q4_K_XL`, sha256 `a7c5bc71…`) on llama.cpp `edd6e2bb`, RTX 3090; and
  `deepseek/deepseek-v4.1-flash` on OpenRouter pinned to `sail-research/fp4`. Reasoning off on both, verified on every
  recorded call.
- **Code.** Orb `8821c071`, harness `7883a8bd`, both clean.
- **Judge.** Jev's three-way `shape` question ([jev_check.py](../jev_check.py)); a reply counts as driven at
  P(driven) ≥ 0.75, pre-registered on the pilot's hand labels. The live preflight labeled all 12 fixtures correctly
  before each scoring pass, and every answer came back from `typesafe/jev-1.13-20260917`. Raw answers are kept beside
  each run's scores.

## Results

| Model | Arm | Clean turns | Driven (P ≥ 0.75) | Jev label: driven / afterthought / static | Mean P(driven) | Mean words |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Gemma | on | 60/60 | 22/60 | 58% / 8% / 33% | 0.54 | 283 |
| Gemma | off | 60/60 | 11/60 | 35% / 22% / 43% | 0.33 | 275 |
| DeepSeek | on | 60/60 | 23/60 | 60% / 17% / 23% | 0.55 | 288 |
| DeepSeek | off | 60/60 | 18/60 | 57% / 18% / 25% | 0.48 | 251 |

Paired difference in driven share, on minus off, bootstrapped over contexts (one per card):

| Model | At the threshold | By Jev's label |
| --- | ---: | ---: |
| Gemma | +0.18 (+0.03 to +0.33) | +0.23 (+0.10 to +0.38) |
| DeepSeek | +0.08 (−0.05 to +0.22) | +0.03 (−0.12 to +0.18) |

- **Length does not explain Gemma's effect.** The arms are within 3% of each other in words, and Director-on is
  driven more often in every pooled length tertile (short 4/17 vs 2/23, middle 4/16 vs 4/23, long 14/27 vs 5/14).
  DeepSeek's on-replies are 15% longer, and on leads only in the middle tertile, so its small difference may be
  length.
- **Contexts that set nothing in motion carry the effect.** On the 40 drafted cards Gemma is driven 13/40 with the
  Director and 4/40 without; on the hand-written 20, several of which already have a truck, ferry or train on its
  way, it is 9/20 and 7/20. DeepSeek: 10/40 vs 9/40 and 13/20 vs 9/20.
- **Judge noise.** Re-asked once, live, 1 of 20 replies changed label and 1 crossed the threshold (0.76 → 0.72);
  P(driven) moved 0.03 on average, 0.08 at most ([c60-noise](c60-noise/noise.json)).
- **Cost and time.** DeepSeek: $0.06 for 120 turns, median 21 s a turn. Gemma: median 8 s.

## What the Directors ask for

Every Director call on both models carried a `next_event`. What differs is what they ask for. A crude keyword count
over the 60 directions: Gemma names an interruption (sudden, bursts, crisis, thunder, shouts…) in 18, DeepSeek in 7.

- **Gemma** reaches for something from outside: "a sudden, momentary dip in power, causing the lights to flicker"
  (ingvild), "a sudden crack of thunder" (ravi), "a heavy, rhythmic thudding from the floor above" (maite), "a
  sudden, frantic phone call" (wren), "a sudden, loud disagreement between two older pickers" (colette).
- **DeepSeek** keeps the scene where it is: "lets the quiet stretch, then offers a small dry story from a past
  winter" (ingvild), "notices the apprentice has stopped working and is watching the regulator" (ambrose), "pours two
  coffees without being asked and carries one over to the piano" (otis). Its real turns, such as the argument at
  bao's chess cart, ravi's phone, the dresser's crisis in lucia and the stranger off eilidh's ferry, are a minority.
  Its Writer already drives 30% of the time with no Director, so there is little room left for a quiet Director to
  show.

## Required fields reach this DeepSeek only through the schema

The Director's `next_event` fragment is marked required. On the pinned upstream, a "Required: keywords, next_event"
line in the request filled it in 1 of 20 calls, and replaying the incomplete call once with the missing fields named
filled 3 of 20 ([full-deepseek](full-deepseek/), [full-deepseek-required](full-deepseek-required/), 20-context runs on
earlier Orb commits). The same recorded requests with `required` on the tool schema filled it in 20 of 20, because
the upstream decodes against the schema. Orb `8821c071` keeps required fragments on the shared `direct_scene` schema;
on this run all 60 DeepSeek calls carried a `next_event`.

## Limits

- **No hand labels on the full run.** The 0.75 threshold was set on the pilot's 10 hand labels and is untested on
  these 240 replies. Jev's own label is reported beside it and agrees in direction on both models.
- **One repeat per context.** With identical code, DeepSeek's Director-off arm gave 8/20 and 5/20 on two earlier
  20-context runs, so a single run carries real sampling noise; the intervals above include it only through the
  spread across contexts.
- **The corpus is chosen where the Director should help.** Every context ends on a passive user turn; the result says
  nothing about turns where the user drives.
- `bench.json` runs every detector, the lorebook and a state fragment, not the out-of-the-box settings.

Per-turn data: [c60-gemma/turns.csv](c60-gemma/turns.csv), [c60-deepseek/turns.csv](c60-deepseek/turns.csv).
The Gemma pilot is in [pilot/](pilot/REPORT.md).
