"""
template_repetition.py — Detect repetitive syntactic structures in LLM output.
"""

import re
from dataclasses import dataclass, field

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
_BE_VERBS = frozenset("is am are was were be been being".split())
_MODALS = frozenset("can could will would shall should may might must".split())
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


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r'(?<=[.!?"""\'])\s+', text.strip())
    return [s.strip() for s in raw if s.strip()]


def detect_template_repetition(
    text: str,
    max_tags: int = 8,
    flag_threshold: int = 2,
    min_tags: int = 4,
) -> TemplateResult:
    sentences = _split_sentences(text)
    total = len(sentences)
    if total == 0:
        return TemplateResult([], {}, 0, 0, 0.0)

    template_sentences: dict[str, list[str]] = {}
    for sent in sentences:
        tokens = _tokenize(sent)
        if len(tokens) < min_tags:
            continue
        tmpl = " ".join(_tag_word(w) for w in tokens[:max_tags])
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