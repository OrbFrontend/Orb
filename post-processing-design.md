# Implementation Plan: ReAct-Style Refinement with Programmatic Detection

## 1. Architecture Overview

Pipeline: `_agent_pass` → `_writer_pass` → `_refine_pass` (new ReAct loop).

`_refine_pass` operates in two stages:
1. **Programmatic Detection:** Runs three scanners on the writer's draft, producing an Audit Report. Zero LLM calls.
2. **Agentic Refinement (ReAct Loop):** The LLM agent receives the Audit Report and uses `refine_apply_patch` to surgically fix the draft. Loops until done or max steps reached.

## 2. Data Foundations

A **phrase bank** (configured in `settings.py` or a JSON file) holds groups of semantically equivalent banned/overused phrases. The first entry in each group is the canonical name.

```python
PHRASE_BANK = [
    ["a mix of", "a mixture of", "a blend of"],
    ["voice dripping", "voice dripping with"],
    ["tension in the air", "thick tension in the air"],
    ...
]
```

## 3. Programmatic Pre-Detection Engine (Zero LLM Cost)

Three scanners run on the draft at the start of `_refine_pass`, producing a consolidated Audit Report.

### 3.1. Banned Phrase Detection (`llm_phrase_detector.py`)
- **Method:** Splits the draft into sentences. For each sentence, generates word-level 3-grams and computes Jaccard similarity against 3-grams of each phrase variant. Flags matches above a configurable threshold (default `0.25`).
- **Output:** `DetectionResult` — list of flagged sentences with matched canonical phrase, matched variant, and score.

### 3.2. Sentence Opener Monotony (`opening_monotony.py`)
- **Method:** Splits draft into sentences. Extracts the first `n_words` (default 3) of each, normalized to lowercase. Flags any opener that appears in ≥ `flag_threshold` fraction of sentences (default 15%) and at least twice.
- **Output:** `MonotonyResult` — flagged openers with counts, fraction, and example sentences.

### 3.3. Syntactic Template Repetition (`template_repetition.py`)
- **Method:** Reduces each sentence to a coarse POS skeleton (e.g., `"PRON VERB DET NOUN PREP NOUN"`) using a lightweight rule-based tagger (no external models). Flags any template appearing ≥ `flag_threshold` times (default 2).
- **Output:** `TemplateResult` — flagged templates with counts, example sentences, and an overall `repetition_score`.

### 3.4. Audit Report Format

```
*** REFINEMENT AUDIT REPORT ***

1. Banned Phrases:
   - "voice dripping" (sentence: "...his voice dripping with contempt...")
   - "tension in the air" (sentence: "...thick tension in the air between them...")

2. Repetitive Openers:
   - "he looked" (3/12 sentences, 25%): "He looked at her.", "He looked away.", "He looked up."

3. Repetitive Templates:
   - "PRON VERB DET NOUN PREP NOUN" (4 sentences): "She crossed the room in silence.", ...

*** END OF REPORT ***
```

## 4. Tool Definitions

### `refine_apply_patch`
- **Description:** Applies one or more exact text replacements to the draft. Each `search` must exactly match current draft text.
- **Parameters:**
  - `patches` (array): ordered list of `{"search": str, "replace": str}`
- **Returns:** Always return success, if failure occurs during operation then skip that item, the updated Audit Report from re-running all three scanners — giving the agent a live view of remaining issues.

## 5. The `_refine_pass` ReAct Loop

### 5.1. Initialization
1. `_writer_pass` completes and buffers the full `draft`.
2. Pre-Detection Engine generates the `Audit Report`.
3. Message context is built for maximum KV cache reuse:
   - `prefix` (System + History)
   - `{"role": "user", "content": effective_msg}`
   - `{"role": "assistant", "content": draft}`
   - `{"role": "system", "content": refine_agent_instructions + "\n" + AuditReport}`

### 5.2. Agent Instructions
> You are the Refinement Agent. Review the draft above and address every issue in the REFINEMENT AUDIT REPORT.
> 1. Use `refine_apply_patch` to replace problematic text with improved phrasing. `search` must exactly match the current draft text.
> 2. After each patch, you will receive an updated Audit Report. Continue until all issues are resolved.
> 3. When done, respond with "AUDIT COMPLETE." and nothing else.

### 5.3. Loop Execution (max 5–7 iterations)
1. Agent reasons about the Audit Report and calls `refine_apply_patch`.
2. Orchestrator applies patches and returns the updated Audit Report (or error).
3. Tool call + result are appended to the thread.
4. Loop ends when agent outputs "AUDIT COMPLETE." or max steps is reached.

## 6. Patch Application Logic

1. **Exact match required.** Search for the `search` string in the current `draft` buffer.
2. **No match:** Return `"Error: '<search>' not found in draft."`
3. **Multiple matches:** Return `"Error: Multiple matches found. Use a more specific 'search' string."` — nudges the agent to add surrounding context.
4. **Single match:** Apply the replacement and return the refreshed Audit Report.

## 7. Pipeline Integration

### 7.1. Buffering
`_writer_pass` fully buffers output before `_refine_pass` begins. The final patched draft is streamed to the user only after `_refine_pass` completes, eliminating jarring UI updates.

### 7.2. `handle_turn` / `handle_regenerate`
Replace old single-shot `_refine_pass` calls with the new orchestrator. Persist the final patched `resp_text` and store the full Audit Report + ReAct trace in `conversation_log` for debugging.

### 7.3. Graceful Fallback
If `_refine_pass` errors or hits max iterations without "AUDIT COMPLETE.", fall back to the unpatched `_writer_pass` draft.

---

## Library Implementations

### `llm_phrase_detector.py`
```python
"""
llm_phrase_detector.py — Detect overused LLM phrases via word-level trigram fuzzy matching.

Usage:
    from llm_phrase_detector import detect_cliches

    phrase_bank = [
        ["a mix of", "a mixture of", "a blend of"],
        ["it's worth noting that", "it is worth mentioning that"],
        ...
    ]

    result = detect_cliches(text, phrase_bank)

    result.flagged_sentences   # list of {sentence, cliches: [{canonical, variant, score}]}
    result.unique_cliches      # sorted list of canonical cliché names found
    result.total_sentences     # total sentence count in text
    result.flagged_count       # number of sentences with at least one hit
"""

import re
from dataclasses import dataclass, field
from nltk import ngrams

_N = 3
_DEFAULT_THRESHOLD = 0.25


# ── Public data structures ───────────────────────────────────────────────

@dataclass
class ClicheHit:
    canonical: str
    variant: str
    score: float

@dataclass
class FlaggedSentence:
    sentence: str
    cliches: list[ClicheHit] = field(default_factory=list)

@dataclass
class DetectionResult:
    flagged_sentences: list[FlaggedSentence]
    unique_cliches: list[str]
    total_sentences: int
    flagged_count: int


# ── Internals ────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _trigrams(tokens: list[str]) -> set[tuple[str, ...]]:
    return set(ngrams(tokens, _N))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def _match_sentence(
    sent_tokens: list[str],
    phrase_bank: list[list[str]],
    threshold: float,
) -> list[ClicheHit]:
    hits: list[ClicheHit] = []

    for variant_group in phrase_bank:
        best: ClicheHit | None = None
        best_score = 0.0

        for variant in variant_group:
            var_tokens = _tokenize(variant)
            var_grams = _trigrams(var_tokens)
            if not var_grams:
                continue

            window_len = len(var_tokens) + 3

            for start in range(max(1, len(sent_tokens) - window_len + 1)):
                window = sent_tokens[start : start + window_len]
                score = _jaccard(var_grams, _trigrams(window))

                if score >= threshold and score > best_score:
                    best_score = score
                    best = ClicheHit(
                        canonical=variant_group[0],
                        variant=variant,
                        score=round(score, 4),
                    )

        if best:
            hits.append(best)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


# ── Public API ───────────────────────────────────────────────────────────

def detect_cliches(
    text: str,
    phrase_bank: list[list[str]],
    threshold: float = _DEFAULT_THRESHOLD,
) -> DetectionResult:
    """
    Scan `text` for overused phrases defined in `phrase_bank`.

    Args:
        text:         Raw LLM output string.
        phrase_bank:  List of variant groups. Each group is a list of strings
                      where the first entry is treated as the canonical name.
                      e.g. [["a mix of", "a mixture of", "a blend of"], ...]
        threshold:    Minimum Jaccard similarity to flag (0.0–1.0, default 0.25).

    Returns:
        DetectionResult with flagged sentences, unique clichés, and counts.
    """
    sentences = _split_sentences(text)
    flagged: list[FlaggedSentence] = []
    all_canonicals: set[str] = set()

    for sentence in sentences:
        tokens = _tokenize(sentence)
        hits = _match_sentence(tokens, phrase_bank, threshold)
        if hits:
            flagged.append(FlaggedSentence(sentence=sentence, cliches=hits))
            all_canonicals.update(h.canonical for h in hits)

    return DetectionResult(
        flagged_sentences=flagged,
        unique_cliches=sorted(all_canonicals),
        total_sentences=len(sentences),
        flagged_count=len(flagged),
    )
```

### `opening_monotony.py`
```python
from __future__ import annotations

"""
opening_monotony.py — Detect repetitive sentence openings in LLM output.

Extracts the first N words of each sentence and flags when the same
opening pattern appears too frequently, a common sign of LLM degradation.

Usage:
    from opening_monotony import detect_opening_monotony

    result = detect_opening_monotony(text, n_words=3, flag_threshold=0.15)

    result.flagged_openers     # openers exceeding threshold, with counts
    result.all_openers         # full frequency table
    result.total_sentences     # sentence count
    result.monotony_score      # 0.0–1.0, higher = more repetitive
"""

import re
from dataclasses import dataclass, field
from collections import Counter


@dataclass
class FlaggedOpener:
    opener: str
    count: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass
class MonotonyResult:
    flagged_openers: list[FlaggedOpener]
    all_openers: dict[str, int]
    total_sentences: int
    monotony_score: float


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def _normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _get_opener(sentence: str, n_words: int) -> str | None:
    words = sentence.split()
    if len(words) < n_words:
        return None
    return " ".join(_normalize(w) for w in words[:n_words])


def detect_opening_monotony(
    text: str,
    n_words: int = 3,
    flag_threshold: float = 0.15,
) -> MonotonyResult:
    """
    Detect repetitive sentence openings.

    Args:
        text:            Raw text to analyze.
        n_words:         How many opening words to compare (default 3).
        flag_threshold:  Fraction of sentences sharing an opener to flag it (default 0.15).

    Returns:
        MonotonyResult with flagged openers, frequency table, and a monotony score.
    """
    sentences = _split_sentences(text)
    total = len(sentences)
    if total == 0:
        return MonotonyResult([], {}, 0, 0.0)

    # Map opener -> sentences
    opener_sentences: dict[str, list[str]] = {}
    for sent in sentences:
        opener = _get_opener(sent, n_words)
        if opener:
            opener_sentences.setdefault(opener, []).append(sent)

    counts = {k: len(v) for k, v in opener_sentences.items()}

    # Flag openers above threshold
    flagged: list[FlaggedOpener] = []
    for opener, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        frac = count / total
        if frac >= flag_threshold and count >= 2:
            flagged.append(FlaggedOpener(
                opener=opener,
                count=count,
                fraction=round(frac, 4),
                sentences=opener_sentences[opener],
            ))

    # Monotony score: what fraction of sentences use a repeated opener?
    repeated_count = sum(c for c in counts.values() if c >= 2)
    monotony_score = round(repeated_count / total, 4) if total else 0.0

    return MonotonyResult(
        flagged_openers=flagged,
        all_openers=counts,
        total_sentences=total,
        monotony_score=monotony_score,
    )
```

### `template_repetition.py`
```python
"""
template_repetition.py — Detect repetitive syntactic structures in LLM output.

Reduces each sentence to a POS skeleton (e.g. "DET NOUN VERB ADV") using a
lightweight rule-based tagger (no external models needed), then flags when
too many sentences share the same template.

Usage:
    from template_repetition import detect_template_repetition

    result = detect_template_repetition(text, flag_threshold=2)

    result.flagged_templates   # templates appearing >= threshold times
    result.all_templates       # full frequency table
    result.repetition_score    # 0.0–1.0, higher = more repetitive
"""

import re
from dataclasses import dataclass, field
from collections import Counter

# ── Lightweight rule-based POS tagger ────────────────────────────────────

_DETERMINERS = frozenset(
    "a an the this that these those my your his her its our their some any no "
    "every each all both few several many much".split()
)
_PRONOUNS = frozenset(
    "i me you he him she her it we us they them myself yourself himself herself "
    "itself ourselves themselves what which who whom whose".split()
)
_PREPOSITIONS = frozenset(
    "in on at to for of from by with about into through during before after "
    "between among above below across along around behind beyond near over "
    "under within without against toward towards upon".split()
)
_CONJUNCTIONS = frozenset(
    "and but or nor yet so because although though while if unless since "
    "whereas whenever wherever however moreover furthermore additionally "
    "nevertheless nonetheless meanwhile therefore thus hence".split()
)
_BE_VERBS = frozenset(
    "is am are was were be been being".split()
)
_MODALS = frozenset(
    "can could will would shall should may might must".split()
)
_COMMON_ADVERBS = frozenset(
    "not very also just still already even now then always never often "
    "sometimes usually really quite rather too particularly especially "
    "increasingly rapidly significantly merely simply".split()
)

_VERB_SUFFIX_RE = re.compile(r"(ed|ing|ize|ise|ify|ate)$")
_ADJ_SUFFIX_RE = re.compile(r"(ful|less|ous|ive|ible|able|ial|ical|ent|ant)$")
_NOUN_SUFFIX_RE = re.compile(r"(tion|sion|ment|ness|ity|ance|ence|ism|ist|er|or|ure)$")


def _tag_word(word: str) -> str:
    w = word.lower()
    if w in _DETERMINERS: return "DET"
    if w in _PRONOUNS: return "PRON"
    if w in _PREPOSITIONS: return "PREP"
    if w in _CONJUNCTIONS: return "CONJ"
    if w in _BE_VERBS: return "VERB"
    if w in _MODALS: return "MOD"
    if w in _COMMON_ADVERBS: return "ADV"
    if _VERB_SUFFIX_RE.search(w): return "VERB"
    if _ADJ_SUFFIX_RE.search(w): return "ADJ"
    if _NOUN_SUFFIX_RE.search(w): return "NOUN"
    return "NOUN"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _get_template(sentence: str, max_tags: int = 8) -> str:
    words = _tokenize(sentence)
    tags = [_tag_word(w) for w in words[:max_tags]]
    return " ".join(tags)


# ── Public data structures ───────────────────────────────────────────────

@dataclass
class FlaggedTemplate:
    template: str
    count: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass
class TemplateResult:
    flagged_templates: list[FlaggedTemplate]
    all_templates: dict[str, int]
    total_sentences: int
    unique_templates: int
    repetition_score: float


# ── Internals ────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


# ── Public API ───────────────────────────────────────────────────────────

def detect_template_repetition(
    text: str,
    max_tags: int = 8,
    flag_threshold: int = 2,
) -> TemplateResult:
    """
    Detect repetitive syntactic templates.

    Args:
        text:            Raw text to analyze.
        max_tags:        POS tags to keep per sentence (default 8).
        flag_threshold:  Minimum occurrences to flag a template (default 2).

    Returns:
        TemplateResult with flagged templates, counts, and repetition score.
    """
    sentences = _split_sentences(text)
    total = len(sentences)
    if total == 0:
        return TemplateResult([], {}, 0, 0, 0.0)

    template_sentences: dict[str, list[str]] = {}
    for sent in sentences:
        tmpl = _get_template(sent, max_tags)
        template_sentences.setdefault(tmpl, []).append(sent)

    counts = {k: len(v) for k, v in template_sentences.items()}

    flagged: list[FlaggedTemplate] = []
    for tmpl, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        if count >= flag_threshold:
            flagged.append(FlaggedTemplate(
                template=tmpl,
                count=count,
                fraction=round(count / total, 4),
                sentences=template_sentences[tmpl],
            ))

    unique = len(counts)
    rep_score = round(1.0 - (unique / total), 4) if total else 0.0

    return TemplateResult(
        flagged_templates=flagged,
        all_templates=counts,
        total_sentences=total,
        unique_templates=unique,
        repetition_score=rep_score,
    )
```
