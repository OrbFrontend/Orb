"""
Contrastive-negation ("AI slop") detector.

Catches rhetorical patterns like:
    "It's not a bug, but a feature."
    "This isn't a setback, it is an opportunity."

Avoids common false positives:
    - "not only … but (also)"
    - infinitive negation ("told him not to go, but …")
    - regular clause contrast ("I'm not sure, but I think …")
    - unrelated be-verb reappearance ("isn't done, but the deadline is …")
    - questions ("Isn't that odd? Where is …")
    - different-subject switches ("He isn't X, she is Y")
"""

import re

# ── helpers ───────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]


def _tokenize(sent: str) -> list[str]:
    return re.findall(r"\w+(?:'\w+)?|[^\s\w]", sent)


_PRONOUNS = frozenset(
    "i me my he him his she her it its we us our they them their "
    "this that these those you your".split()
)
_BE_VERBS = frozenset("is am are was were be been being".split())
_CONJUNCTIONS = frozenset("but and or yet so".split())
_CLAUSE_SIGNALS = frozenset(
    "i he she we they you who which what where when why how if because "
    "since although though while do did does can could will would shall "
    "should may might must have has had".split()
) | _PRONOUNS


def _tag_word(word: str) -> str:
    low = word.lower()
    if low in _BE_VERBS:
        return "VERB"
    if low in ("a", "an", "the"):
        return "DET"
    if low in ("not", "n't"):
        return "NEG"
    if low in _CONJUNCTIONS:
        return "CONJ"
    if low in _PRONOUNS:
        return "PRON"
    if low.endswith("ly"):
        return "ADV"
    if low.endswith(("tion", "ment", "ness", "ity", "ure")):
        return "NOUN"
    if low.endswith(("ing", "ed")):
        return "VERB"
    if low.endswith(("ful", "ous", "ive", "ble", "al", "ent", "ant")):
        return "ADJ"
    return "NOUN"


# ── constants ─────────────────────────────────────────────────────────────────

_NEGATED_BE = frozenset({
    "isn't", "aren't", "wasn't", "weren't",
    "is not", "are not", "was not", "were not",
})
_SAME_SUBJECT_PRONOUNS = frozenset("it this that".split())


# ── guard helpers ─────────────────────────────────────────────────────────────

def _strip_trailing_punct(tokens: list[str], tags: list[str]):
    """Remove sentence-final punctuation from token/tag lists (in-place)."""
    while tokens and tokens[-1] in ".!?,;:":
        tokens.pop()
        tags.pop()


def _is_not_only(lowers: list[str], not_idx: int) -> bool:
    return not_idx + 1 < len(lowers) and lowers[not_idx + 1] == "only"


def _is_infinitive_not(lowers: list[str], not_idx: int) -> bool:
    return not_idx + 1 < len(lowers) and lowers[not_idx + 1] == "to"


def _x_looks_like_clause(x_tokens: list[str]) -> bool:
    """True if the span between 'not' and 'but' looks like a full clause
    rather than a short noun/adj complement."""
    x_lower = {t.lower() for t in x_tokens}
    if x_lower & _CLAUSE_SIGNALS:
        return True
    content = [t for t in x_tokens if t.lower() not in ("a", "an", "the", ",")]
    return len(content) > 5


def _y_looks_like_clause(y_tokens: list[str]) -> bool:
    """True if Y after 'but' opens with its own subject, making it an
    independent clause rather than a bare complement."""
    if not y_tokens:
        return False
    first = y_tokens[0].lower()
    return first in _CLAUSE_SIGNALS and first not in ("this", "that")


# ── Strategy 2: negated be-verb … affirmative be-verb ────────────────────────

def _find_negated_be_pattern(tokens: list[str], tags: list[str]) -> dict | None:
    """Match 'isn't X, ... is Y' but only when the affirmative clause
    shares the same (or anaphoric) subject."""

    neg_idx = None
    neg_width = 1
    for i, t in enumerate(tokens):
        if t in ("isn't", "aren't", "wasn't", "weren't"):
            neg_idx = i
            break
        if (i + 1 < len(tokens) and tags[i] == "VERB"
                and tokens[i + 1] == "not"
                and f"{tokens[i]} not" in _NEGATED_BE):
            neg_idx = i
            neg_width = 2
            break

    if neg_idx is None:
        return None

    boundary = None
    for i in range(neg_idx + neg_width + 1, len(tokens)):
        if tokens[i] in (",", ";") or tokens[i] == "but":
            boundary = i
            break

    if boundary is None:
        return None

    aff_idx = None
    for i in range(boundary + 1, len(tokens)):
        if tokens[i] in _BE_VERBS:
            aff_idx = i
            break

    if aff_idx is None:
        return None

    # Subject-continuity check: allow anaphoric pronouns, same subject word,
    # or elided subject (be-verb right after boundary).
    neg_subject = tokens[neg_idx - 1] if neg_idx > 0 else None
    if aff_idx > boundary + 1:
        pre_aff = tokens[aff_idx - 1]
        same_subject = (
            pre_aff in _SAME_SUBJECT_PRONOUNS
            or (neg_subject is not None and pre_aff == neg_subject)
        )
        if not same_subject:
            return None

    x_tokens = tokens[neg_idx + neg_width : boundary]
    x_tags   = tags  [neg_idx + neg_width : boundary]
    y_tokens = tokens[aff_idx + 1 :]
    y_tags   = tags  [aff_idx + 1 :]

    _strip_trailing_punct(x_tokens, x_tags)
    _strip_trailing_punct(y_tokens, y_tags)

    if x_tags and y_tags:
        return {
            "x_template": " ".join(x_tags),
            "y_template": " ".join(y_tags),
            "is_parallel": x_tags == y_tags,
        }
    return None


# ── Strategy 1: "not … but …" ────────────────────────────────────────────────

def _find_not_but_pattern(lowers: list[str], words: list[str],
                          tags: list[str]) -> dict | None:
    not_idx = but_idx = None
    for i, w in enumerate(lowers):
        if w == "not" and not_idx is None:
            not_idx = i
        if w == "but" and not_idx is not None and i > not_idx + 1:
            but_idx = i
            break

    if not_idx is None or but_idx is None:
        return None

    if _is_not_only(lowers, not_idx):
        return None
    if _is_infinitive_not(lowers, not_idx):
        return None

    x_tokens = words[not_idx + 1 : but_idx]
    y_tokens = words[but_idx + 1 :]
    x_tags   = tags [not_idx + 1 : but_idx]
    y_tags   = tags [but_idx + 1 :]

    _strip_trailing_punct(x_tokens, x_tags)
    _strip_trailing_punct(y_tokens, y_tags)

    if not x_tags or not y_tags:
        return None
    if _x_looks_like_clause(x_tokens):
        return None
    if _y_looks_like_clause(y_tokens):
        return None

    return {
        "x_template": " ".join(x_tags),
        "y_template": " ".join(y_tags),
        "is_parallel": x_tags == y_tags,
    }


# ── main entry point ──────────────────────────────────────────────────────────

def detect_contrastive_negation(text: str) -> list[dict]:
    """Find 'Not X, but Y' and 'isn't X, it is Y' rhetorical patterns.

    Returns a list of dicts with keys:
        sentence, x_template, y_template, is_parallel
    """
    sentences = _split_sentences(text)
    results = []

    for sent in sentences:
        words = _tokenize(sent)
        if len(words) < 4:
            continue

        tags   = [_tag_word(w) for w in words]
        lowers = [w.lower() for w in words]

        if sent.rstrip().endswith("?"):
            continue

        hit = _find_not_but_pattern(lowers, words, tags)
        if hit is None:
            hit = _find_negated_be_pattern(lowers, tags)

        if hit:
            hit["sentence"] = sent
            results.append(hit)

    return results
