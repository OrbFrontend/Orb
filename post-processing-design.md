Okay, you're right. Automating detection is key for efficiency, and keeping the model's job to simple search and replace is more robust. Let's refine this plan.

---

# Implementation Plan: ReAct-Style Refinement with Advanced Programmatic Detection

## 1. Architecture Overview

The pipeline remains: `_agent_pass` (for styles/prompt rewrite), `_writer_pass` (for core response generation), and `_refine_pass` (the new ReAct loop for post-processing).

The `_refine_pass` now operates as follows:
1.  **Programmatic Detection:** An orchestrator component runs advanced scans on the initial writer's draft and relevant conversation history. It generates a comprehensive "Audit Report" detailing banned words, repetitive phrases, and structural issues. This step requires *no LLM calls*.
2.  **Agentic Refinement (ReAct Loop):** The LLM agent receives this Audit Report. Its task is then to *act* on the report using simple `find_text` and `apply_patch` tools to surgically correct the draft. The loop iterates until the agent signals completion or a max step limit is reached.

This approach ensures efficiency for detection and robustness for correction, focusing the LLM on decision-making and precise action.

## 2. Data Foundations

### 2.1. Manual Ban List
-   A configuration list (e.g., in `settings.py` or a dedicated JSON file) will hold manually curated words and phrases to avoid.
-   Example: `["purr", "velvety", "predatory", "voice dripping", "tension in the air"]`
-   This list is used directly by the programmatic detection engine.

## 3. Programmatic Pre-Detection Engine (Zero LLM Cost)

This is a new, dedicated function called at the start of `_refine_pass`. It takes the current draft and the last `N` assistant messages (e.g., 3-5) as input and outputs a structured Audit Report.

### 3.1. Ban List Match
-   **Method:** Iterates through the manual ban list. For each entry, performs a fuzzy substring search (e.g., Python's nltk with a certain threshold.
-   **Output:** List of `{"text": "matched_banned_phrase", "location_context": "snippet of draft containing it"}`.

### 3.2. Cross-Turn Phrase Overlap (3-grams and 4-grams)
-   **Purpose:** Detects exact phrasal repetition between the *current draft* and *recent historical assistant messages*.
-   **Method:**
    1.  **History Preprocessing:**
        *   Take the last `lookback_messages` (configurable, default 3-5) assistant messages from the conversation history.
        *   Tokenize each historical message into words.
        *   Generate all 3-grams and 4-grams from these historical messages.
        *   Store these N-grams in a **hash set** for `O(1)` average lookup time. This is done once per refinement pass.
    2.  **Draft Scanning:**
        *   Tokenize the current draft into words.
        *   Generate all 3-grams and 4-grams from the current draft.
        *   For each draft N-gram, check if it exists in the pre-computed hash set of historical N-grams.
        *   If a match is found, record the draft N-gram and its context.
-   **Performance/Complexity:**
    *   **History Preprocessing:** `O(L_history)` where `L_history` is the total word count across all historical messages being considered. This is very fast for 3-5 messages.
    *   **Draft Scanning:** `O(L_draft * 1)` (average for hash set lookup) where `L_draft` is the word count of the current draft. This is linear with the draft's length, making it extremely efficient even for long drafts.
    *   **Conclusion:** This method is highly performant and will not be an issue for typical LLM output lengths.
-   **Output:** List of `{"phrase": "repeated_phrase", "in_draft_context": "snippet from draft", "in_history_context": "snippet from history message N"}`.

### 3.3. Sentence Opener Analysis
-   **Purpose:** Identifies repetitive sentence beginnings within the draft and compared to history.
-   **Method:**
    1.  Split the current draft into sentences.
    2.  For each sentence, extract the first 2-3 words (normalized to lowercase).
    3.  Count frequencies of these openers within the draft. Flag if any single opener appears more than `X` times (e.g., 2 or 3 times).
    4.  Repeat steps 1-2 for the `lookback_messages` assistant messages.
    5.  Compare draft openers to historical openers. Flag if an opener used `Y` times in the draft also appeared `Z` times in history (e.g., Y=2, Z=2).
-   **Output:** List of `{"opener": "The man", "count_in_draft": 3, "examples": ["The man walked...", "The man said...", "The man looked..."]}` and `{"opener": "She knew", "count_in_draft": 2, "appeared_in_history_n_times": 3}`.

### 3.4. Audit Report Generation
-   Consolidate all findings from 3.1, 3.2, and 3.3 into a single, structured JSON or plain text report that will be presented to the LLM agent.
-   **Example Report Structure (simplified for LLM consumption):**
    ```
    *** REFINEMENT AUDIT REPORT ***

    ISSUES FOUND IN DRAFT:

    1. Banned Phrases:
       - "velvety touch" (found in: "...a velvety touch upon her arm...")
       - "tension in the air" (found in: "...thick tension in the air between them...")

    2. Repetitive Phrases (N-grams vs. History):
       - "took a deep breath" (found in draft: "...took a deep breath before answering...", also in history: "She took a deep breath before...")
       - "voice dropped to a whisper" (found in draft: "...his voice dropped to a whisper...", also in history: "Her voice dropped to a whisper as...")

    3. Repetitive Sentence Openers:
       - "He looked" (used 3 times in draft: "...He looked at her...", "...He looked away...", "...He looked up.")
       - "She felt a" (used 2 times in draft, also used in previous assistant message.)

    *** END OF REPORT ***
    ```

## 4. Tool Definitions (for the Refinement Agent)

These tools are designed to be simple, precise, and deterministic, empowering the LLM to *act* on the Audit Report.

### 4.1. `refine_find_text`
-   **Description:** Searches the *current* version of the draft for an exact text snippet. This helps the agent locate and confirm the context of an issue reported in the audit, or verify its own hunches.
-   **Parameters:**
    -   `text` (string): The exact text to find.
-   **Output:** A string containing the matched text with its surrounding context (e.g., `Match 1: "...snippet before [text] snippet after..."`) or `"No exact match found."`
    *Note: This tool is for verification and context, not fuzzy matching.*

### 4.2. `refine_apply_patch`
-   **Description:** Replaces a specific text segment in the current draft. Use this to fix identified banned phrases, repetitive N-grams, or to rephrase repetitive sentence openers. The `search` parameter must exactly match the text currently in the draft.
-   **Parameters:**
    -   `search` (string): The *exact* text string to be replaced in the draft.
    -   `replace` (string): The new text to substitute.
    -   `reason` (string): A brief explanation for the change (helps in debugging/logging).
-   **Output:** A simple string: `"Success: Patch applied."` or `"Error: 'search' text not found in draft, or multiple matches found. Please be more specific."`

## 5. The `_refine_pass` ReAct Loop

### 5.1. Loop Initialization & KV Cache Reuse
1.  The `_writer_pass` completes and provides the full `draft` content.
2.  The Programmatic Pre-Detection Engine generates the `Audit Report`.
3.  The message context for the ReAct loop is built on the existing prefix:
    *   `prefix` (System + History)
    *   `{"role": "user", "content": effective_msg}`
    *   `{"role": "assistant", "content": draft}` (The full buffered output from the writer)
    *   `{"role": "system", "content": refine_agent_instructions + "\n" + AuditReport}` (The instructions for the refinement agent, *plus* the fully detailed Audit Report).
    This structure ensures maximum KV cache reuse for the `_refine_pass`, as only the new system message and subsequent ReAct turns are novel.

### 5.2. Refinement Agent Instructions
The `refine_agent_instructions` system message guides the LLM:
-   "You are the Refinement Agent. Your goal is to review the assistant's previous response (the draft above) and improve its quality based on the `REFINEMENT AUDIT REPORT` provided below.
-   **Your Task:**
    1.  Address each issue listed in the `REFINEMENT AUDIT REPORT`.
    2.  Use `refine_find_text` to locate the exact phrase and its context if you need to confirm.
    3.  Use `refine_apply_patch` to replace problematic text with improved phrasing. Ensure `search` is an exact match for the current text.
    4.  When you believe all issues are resolved, respond with "AUDIT COMPLETE." without calling any further tools."
-   The Audit Report follows these instructions directly.

### 5.3. Step Execution (Max 5–7 iterations)
1.  **Assistant Turn:** The LLM agent reasons about an issue from the Audit Report and decides whether to use `refine_find_text` (to confirm context) or `refine_apply_patch` (to fix).
2.  **Tool Execution (Orchestrator):** The orchestrator executes the called tool using the rules defined in Section 4.
3.  **Append Results:** The agent's tool call(s) and the orchestrator's plain-text tool output(s) are appended to the ReAct message thread.
4.  **Termination Check:** If the assistant's response is exactly "AUDIT COMPLETE." or the maximum step limit is reached, the loop terminates.

## 6. Patch Application Logic

When the orchestrator handles `refine_apply_patch`:

1.  **Exact Match:** The system attempts to find an *exact* match for the `search` string within the current, mutable `draft` buffer.
2.  **Conflict Handling:**
    *   If no match is found, return `"Error: 'search' text not found in draft."`
    *   If multiple *exact* matches are found, return `"Error: Multiple exact matches found. Please provide a more specific 'search' string for unique identification."` (This nudges the LLM to make its `search` precise, perhaps by including surrounding words).
3.  **Apply:** If a single, exact match is found, the replacement is performed in the `draft` buffer.
4.  **Response:** The plain-text success/failure message is returned to the agent.

## 7. Pipeline Integration

### 7.1. Buffering the Draft
-   The `_writer_pass` will *not* stream tokens directly to the user. Instead, it will fully buffer its entire output.
-   The `_refine_pass` (with its ReAct loop) then operates on this buffered draft.
-   Once the `_refine_pass` completes (agent signals "AUDIT COMPLETE." or max steps reached), the *final, patched draft* is then streamed to the user. This completely eliminates jarring UI updates.

### 7.2. Updating `handle_turn` and `handle_regenerate`
-   The calls to the old single-shot `_refine_pass` are replaced with the new orchestrator that encapsulates the programmatic detection and the ReAct loop.
-   Database persistence:
    -   The final `resp_text` will be the version after all patches.
    -   The `conversation_log` should store the full `Audit Report` and the sequence of tool calls and outputs from the ReAct loop for transparency and debugging.

### 7.3. Graceful Fallback
-   If the `_refine_pass` encounters an unexpected error or hits its maximum iteration limit without the agent signaling completion, the orchestrator will default to using the original, un-patched draft generated by the `_writer_pass`. This prevents broken or incomplete responses from being sent to the user.

fuzzy search library code:
```
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