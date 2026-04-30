"""
Contrastive-negation ("AI slop") detector.

Catches rhetorical patterns like:
    "It's not a bug, but a feature."
    "This isn't a setback, it is an opportunity."
    "He doesn't just give up; he breaks down."

Avoids common false positives:
    - "not only … but (also)"
    - infinitive negation ("told him not to go, but …")
    - regular clause contrast ("I'm not sure, but I think …")
    - unrelated be-verb reappearance ("isn't done, but the deadline is …")
    - questions ("Isn't that odd? Where is …")
    - different-subject switches ("He isn't X, she is Y")
"""

from __future__ import annotations
import re

# ── helpers ───────────────────────────────────────────────────────────────────


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.!?])["”]?\s+', text) if s.strip()]


def _tokenize(sent: str) -> list[str]:
    return re.findall(r"\w+(?:'\w+)?|[^\s\w]", sent)


_PRONOUNS = frozenset(
    "i me my he him his she her it its we us our they them their "
    "this that these those you your".split()
)
_BE_VERBS = frozenset("is am are was were be been being 's 're 'm".split())
_DO_VERBS = frozenset("do does did".split())
_HAVE_VERBS = frozenset("have has had".split())
_CONJUNCTIONS = frozenset("but and or yet so".split())
_COMMON_VERBS = frozenset(
    "go goes went gone come comes came take takes took taken "
    "see sees saw seen look looks looked make makes made "
    "give gives gave given get gets got gotten have has had "
    "do does did done say says said tell tells told "
    "know knows knew known think thinks thought feel feels felt "
    "find finds found leave leaves left keep keeps kept "
    "let lets lose loses lost run runs ran "
    "sit sits sat stand stands stood understand understands understood "
    "begin begins began begun drink drinks drank drunk "
    "write writes wrote written speak speaks spoke spoken "
    "break breaks broke broken choose chooses chose chosen "
    "want wants wanted use uses used work works worked "
    "call calls called try tries tried ask asks asked "
    "need needs needed seem seems seemed help helps helped "
    "show shows showed turn turns turned start starts started "
    "play plays played move moves moved live lives lived "
    "believe believes believed bring brings brought "
    "happen happens happened hear hears heard "
    "buy buys bought catch catches caught teach teaches taught "
    "spend spends spent build builds built send sends sent "
    "mean means meant pay pays paid hold holds held "
    "fall falls fell meet meets met lead leads led "
    "win wins won".split()
)
_CLAUSE_SIGNALS = (
    frozenset(
        "i he she we they you who which what where when why how if because "
        "since although though while do did does can could will would shall "
        "should may might must have has had".split()
    )
    | _PRONOUNS
)


def _tag_word(word: str) -> str:
    low = word.lower()
    if low in _BE_VERBS:
        return "VERB"
    if low in _DO_VERBS:
        return "VERB"
    if low in ("a", "an", "the"):
        return "DET"
    if low in ("not", "n't") or low.endswith("n't"):
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
    if low.endswith(("s", "es")) and not low.endswith(("ss", "us", "is", "as", "os")):
        return "VERB"
    if low.endswith(("ful", "ous", "ive", "ble", "al", "ent", "ant")):
        return "ADJ"
    if low in _COMMON_VERBS:
        return "VERB"
    return "NOUN"


# ── constants ─────────────────────────────────────────────────────────────────

_NEGATED_BE = frozenset(
    {
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
        "is not",
        "are not",
        "was not",
        "were not",
        "am not",
        "'s not",
        "'re not",
        "'m not",
    }
)
_NEGATED_DO_CONTRACTIONS = frozenset({"doesn't", "don't", "didn't"})
_NEGATED_HAVE_CONTRACTIONS = frozenset({"hasn't", "haven't", "hadn't"})
_SAME_SUBJECT_PRONOUNS = frozenset("it this that".split())
_PERSONAL_PRONOUNS = frozenset("i me he him she her we us they them you".split())


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


def _y_looks_like_clause(y_tokens: list[str], exclude_it: bool = False) -> bool:
    """True if Y opens with its own subject, making it an independent clause
    rather than a bare complement.  Pass exclude_it=True when Y is a verb
    complement (do-support context), where 'it' is unambiguously an object."""
    if not y_tokens:
        return False
    first = y_tokens[0].lower()
    exclusions = ("this", "that", "it") if exclude_it else ("this", "that")
    return first in _CLAUSE_SIGNALS and first not in exclusions


# ── Strategy 2: negated be-verb … affirmative be-verb ────────────────────────


def _find_negated_be_pattern(tokens: list[str], tags: list[str]) -> dict | None:
    """Match 'isn't X, ... is Y' but only when the affirmative clause
    shares the same (or anaphoric) subject."""

    neg_idx = None
    neg_width = 1
    for i, t in enumerate(tokens):
        if t.lower() in ("isn't", "aren't", "wasn't", "weren't"):
            neg_idx = i
            break
        if (
            i + 1 < len(tokens)
            and tags[i] == "VERB"
            and tokens[i + 1].lower() == "not"
            and f"{tokens[i].lower()} not" in _NEGATED_BE
        ):
            neg_idx = i
            neg_width = 2
            break

    if neg_idx is None:
        return None

    boundary = None
    for i in range(neg_idx + neg_width + 1, len(tokens)):
        if tokens[i] in (",", ";", "—", "–") or tokens[i] == "but":
            boundary = i
            break

    if boundary is None:
        return None

    aff_idx = None
    for i in range(boundary + 1, len(tokens)):
        if tokens[i].lower() in _BE_VERBS:
            aff_idx = i
            break

    if aff_idx is None:
        return None

    neg_subject = tokens[neg_idx - 1] if neg_idx > 0 else None
    if aff_idx > boundary + 1:
        pre_aff = tokens[aff_idx - 1]
        same_subject = (
            pre_aff.lower() in _SAME_SUBJECT_PRONOUNS
            and (neg_subject is None or neg_subject.lower() not in _PERSONAL_PRONOUNS)
        ) or (neg_subject is not None and pre_aff.lower() == neg_subject.lower())
        if not same_subject:
            return None

    x_tokens = tokens[neg_idx + neg_width : boundary]
    x_tags = tags[neg_idx + neg_width : boundary]
    y_tokens = tokens[aff_idx + 1 :]
    y_tags = tags[aff_idx + 1 :]

    _strip_trailing_punct(x_tokens, x_tags)
    _strip_trailing_punct(y_tokens, y_tags)

    if x_tags:
        return {
            "x_template": " ".join(x_tags),
            "y_template": " ".join(y_tags),
            "is_parallel": x_tags == y_tags,
        }
    return None


# ── Strategy 3: do-support ───────────────────────────────────────────────────


def _find_do_support_pattern(
    tokens: list[str], tags: list[str], lowers: list[str]
) -> dict | None:
    """Match 'doesn't/hasn't X, ... [it] Ys' but only when the affirmative clause
    shares the same subject and opens with a verb."""

    neg_idx = None
    neg_width = 1
    for i, t in enumerate(tokens):
        low = t.lower()
        if low in _NEGATED_DO_CONTRACTIONS or low in _NEGATED_HAVE_CONTRACTIONS:
            neg_idx = i
            break
        if (
            i + 1 < len(tokens)
            and tags[i] == "VERB"
            and (low in _DO_VERBS or low in _HAVE_VERBS)
            and tokens[i + 1].lower() == "not"
        ):
            neg_idx = i
            neg_width = 2
            break

    if neg_idx is None:
        return None

    boundary = None
    for i in range(neg_idx + neg_width + 1, len(tokens)):
        if tokens[i] in (",", ";", "—", "–") or tokens[i] == "but":
            boundary = i
            break

    if boundary is None:
        return None

    neg_subject = tokens[neg_idx - 1] if neg_idx > 0 else None

    aff_verb_idx = None
    for i in range(boundary + 1, len(tokens)):
        if tags[i] == "VERB":
            aff_verb_idx = i
            break

    if aff_verb_idx is None:
        return None

    # A conjunction between the boundary and the verb signals an independent clause
    # ("do not like rain, but I brought …"), not a bare complement.
    if any(
        tokens[j].lower() in _CONJUNCTIONS for j in range(boundary + 1, aff_verb_idx)
    ):
        return None

    # Subject-continuity check
    same_subject = False

    # 1. Explicit subject right before the verb
    if aff_verb_idx > boundary + 1:
        pre_verb = tokens[aff_verb_idx - 1]
        if pre_verb in _SAME_SUBJECT_PRONOUNS or (
            neg_subject is not None and pre_verb.lower() == neg_subject.lower()
        ):
            same_subject = True

    # 2. Elided subject (verb immediately after boundary, or boundary + conjunction)
    if not same_subject:
        if aff_verb_idx == boundary + 1:
            same_subject = True
        elif (
            aff_verb_idx == boundary + 2
            and tokens[boundary + 1].lower() in _CONJUNCTIONS
        ):
            same_subject = True

    if not same_subject:
        return None

    x_tokens = tokens[neg_idx + neg_width : boundary]
    x_tags = tags[neg_idx + neg_width : boundary]

    # For "has/have/had + participle", skip the auxiliary so y spans the complement only.
    y_start = aff_verb_idx + 1
    if (
        lowers[aff_verb_idx] in _HAVE_VERBS
        and y_start < len(tags)
        and tags[y_start] == "VERB"
    ):
        y_start += 1

    y_tokens = tokens[y_start:]
    y_tags = tags[y_start:]

    _strip_trailing_punct(x_tokens, x_tags)
    _strip_trailing_punct(y_tokens, y_tags)

    if not x_tags:
        return None
    if _x_looks_like_clause(x_tokens):
        return None
    if _y_looks_like_clause(y_tokens, exclude_it=True):
        return None

    return {
        "x_template": " ".join(x_tags),
        "y_template": " ".join(y_tags),
        "is_parallel": x_tags == y_tags,
    }


# ── Strategy 1: "not … but …" ────────────────────────────────────────────────


def _find_not_but_pattern(
    lowers: list[str], words: list[str], tags: list[str]
) -> dict | None:
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
    x_tags = tags[not_idx + 1 : but_idx]
    y_tags = tags[but_idx + 1 :]

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

_BE_CONTRACTION_STARTERS = frozenset(
    {
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "that",
        "this",
        "there",
        "here",
        "who",
        "what",
        "where",
        "when",
        "how",
    }
)


def _split_contractions(tokens: list[str]) -> list[str]:
    """Split pronoun+be contractions:  she's → she 's,  they're → they 're."""
    result = []
    for token in tokens:
        low = token.lower()
        split = False
        for suffix in ("'s", "'re", "'m"):
            if low.endswith(suffix) and len(token) > len(suffix):
                stem = token[: -len(suffix)]
                if stem.lower() in _BE_CONTRACTION_STARTERS:
                    result.append(stem)
                    result.append(suffix)
                    split = True
                    break
        if not split:
            result.append(token)
    return result


def detect_contrastive_negation(text: str) -> list[dict]:
    """Find 'Not X, but Y', 'isn't X, it is Y', and 'doesn't X, it Ys' rhetorical patterns.

    Returns a list of dicts with keys:
        sentence, x_template, y_template, is_parallel
    """
    sentences = _split_sentences(text)
    results = []

    for sent in sentences:
        words = _split_contractions(_tokenize(sent))
        if len(words) < 4:
            continue

        tags = [_tag_word(w) for w in words]
        lowers = [w.lower() for w in words]

        if sent.rstrip().endswith("?"):
            continue

        hit = _find_not_but_pattern(lowers, words, tags)
        if hit is None:
            hit = _find_negated_be_pattern(words, tags)
        if hit is None:
            hit = _find_do_support_pattern(words, tags, lowers)

        if hit:
            hit["sentence"] = sent
            results.append(hit)

    return results
