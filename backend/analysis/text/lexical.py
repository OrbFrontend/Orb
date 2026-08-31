"""Shared token and sequence helpers for prose detectors."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator

__all__ = [
    "TOKEN_RE",
    "tokenize",
    "normalize_word",
    "ngrams",
    "longest_common_run",
    "is_contiguous_subsequence",
    "STOPWORDS",
    "count_content_words",
]


TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Return lowercase Unicode word tokens."""
    folded = unicodedata.normalize("NFC", text).casefold()
    return [token.replace("’", "'") for token in TOKEN_RE.findall(folded)]


def normalize_word(word: str) -> str:
    """Normalize one whitespace-delimited word."""
    return "".join(tokenize(word))


def ngrams(tokens: list[str], n: int) -> Iterator[tuple[str, ...]]:
    """Yield each contiguous n-word window."""
    if n <= 0:
        raise ValueError("n must be positive")
    for i in range(len(tokens) - n + 1):
        yield tuple(tokens[i : i + n])


def longest_common_run(a: list[str], b: list[str]) -> list[str]:
    """Return the longest contiguous token run shared by *a* and *b*."""
    if not a or not b:
        return []
    best_len = 0
    best_end = 0  # exclusive end index into a
    prev = [0] * (len(b) + 1)
    for i, atok in enumerate(a, start=1):
        curr = [0] * (len(b) + 1)
        for j, btok in enumerate(b, start=1):
            if atok == btok:
                run = prev[j - 1] + 1
                curr[j] = run
                if run > best_len:
                    best_len = run
                    best_end = i
        prev = curr
    return a[best_end - best_len : best_end]


def is_contiguous_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """Return whether *short* is a strict contiguous sub-run of *long*."""
    if not short or len(short) >= len(long):
        return False
    for i in range(len(long) - len(short) + 1):
        if long[i : i + len(short)] == short:
            return True
    return False


_STOPWORD_GROUPS = (
    "a an the this that these those some any each every all both either neither such another other same own",
    "much many more most less least few fewer several enough",
    "and or but nor yet so if then than as while because since though although unless until till whereas whether",
    "whenever wherever",
    "of to in on at by for with from into onto about off out up down over under above below between among through",
    "throughout during before after against without within upon toward towards across along behind beside besides",
    "near around amid amongst beneath beyond per via",
    "is are was were be been being am do does did has have had will would shall should can could may might must ought",
    "having get gets got getting",
    "i you he she it we they his her its their our your my mine him them us me yours hers ours theirs myself yourself",
    "yourselves himself herself itself oneself ourselves themselves one ones someone somebody something anyone anybody",
    "anything everyone everybody everything nobody nothing none whoever whatever whichever whomever",
    "what which who whom whose when where why how",
    "not no just only even also very still now there here really right too quite rather almost already indeed perhaps",
    "maybe anyway instead however moreover thus hence therefore else ever never always often sometimes usually again",
    "once twice well okay ok yes yeah yep nope oh ah uh um hmm hey please actually literally basically simply merely",
    "i'm you're he's she's it's we're they're i've you've we've they've i'll you'll he'll she'll we'll they'll i'd you'd",
    "he'd she'd we'd they'd don't doesn't didn't won't can't cannot couldn't wouldn't shouldn't mustn't isn't aren't",
    "wasn't weren't haven't hasn't hadn't ain't let's that's there's here's what's who's where's when's why's how's",
)
STOPWORDS = frozenset(word for group in _STOPWORD_GROUPS for word in group.split())


def count_content_words(tokens: Iterable[str]) -> int:
    """Count how many tokens are content words (i.e. not stopwords)."""
    return sum(1 for t in tokens if t not in STOPWORDS)
