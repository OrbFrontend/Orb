"""The prompt contract, and the text repairs applied on the way back out.

THE PROMPT IS A PROPERTY OF THE WEIGHTS, not a setting. `serve_prompt` builds
the exact three-block string the pool was written in, which is also what each
model repo's `chat_template.jinja` produces; `tests/unit/test_prose_rewriter_text.py`
pins it byte-for-byte, which is the only way a hand edit here would be noticed.
The `edit` block is fixed at `match` -- the band that rewrites in place. Serving
these weights with no block at all measured del:ins 19.0 against 2.8 at `match`,
worse than a control trained without the block ever having existed.

The REPAIRS are a property of the corpus rather than of the model. AO3's scrape
drops inter-sentence spaces and whole paragraph breaks, so the targets carried
welded dialogue and the model learned to emit some of it; the three functions
below undo the specific defect each comment describes.
"""

from __future__ import annotations

import re

EDIT_MODE = "match"


def serve_prompt(source: str) -> str:
    """The three-block prompt, exactly as the pool was written."""
    return f"<|im_start|>source\n{source}<|im_end|>\n<|im_start|>edit\n{EDIT_MODE}<|im_end|>\n<|im_start|>rewrite\n"


# ANY newline run, not just blank lines -- the corpus builder split on r"\n+" and
# the training target IS one such paragraph. Splitting on blank lines only handed
# the model multi-line blocks whose newlines it had never once been asked to
# emit (0 of 21,942 targets contain one), so it deleted them and welded the
# lines: '"What in the-"\n"Good Lord,"' came back as '"What in the-""Good Lord,"'.
PARAGRAPH_BREAK = re.compile(r"(\n+)")

# Below this the model is out of its training distribution: the corpus builder
# dropped every human paragraph under 80 bytes, so short dialogue lines were
# never targets. Asked to rewrite one anyway it pads toward its learned length and
# invents. Passing them through unchanged is the honest answer.
MIN_REWRITE_BYTES = 80
# 512, because the longest source in the training pool is 445 tokens (p99 275,
# p50 76). Accepting several times that degrades quietly instead of erroring.
MAX_SOURCE_TOKENS = 512
MAX_PARAGRAPHS = 96
MAX_CHARS = 40_000


def plan(text: str) -> list[tuple[str, str]]:
    """Split a paste into ('keep', literal) and ('rewrite', paragraph) pieces.

    The layout is produced BEFORE anything runs, because the paragraphs are
    generated simultaneously and come back out of order; the client needs the
    slots up front to render each one where it belongs.
    """
    pieces: list[tuple[str, str]] = []
    for piece in PARAGRAPH_BREAK.split(text.strip()):
        if not piece:
            continue
        clean = piece.strip()
        if PARAGRAPH_BREAK.fullmatch(piece) or not clean or len(clean.encode()) < MIN_REWRITE_BYTES:
            pieces.append(("keep", piece))  # below the trained floor, or a break
        else:
            pieces.append(("rewrite", clean))
    return pieces


def trim_to_sentence(text: str) -> str:
    """Cut back to the last sentence that actually ended.

    Closing quotes and asterisks may follow the terminal mark, so the cut lands
    after them, not before -- trimming to the '.' inside '..."' would strip the
    quote and unbalance the dialogue this exists to protect.
    """
    closers = "\"'”’*)]"
    best = -1
    for i, ch in enumerate(text):
        # An em-dash ends a sentence only when a closing quote follows it: that
        # is interrupted dialogue ("What in the-"), which the corpus preserves
        # 96.3% of the time. A bare dash between words is a parenthetical.
        terminal = ch in ".!?…" or (ch in "—–" and i + 1 < len(text) and text[i + 1] in closers)
        if terminal:
            j = i + 1
            while j < len(text) and text[j] in closers:
                j += 1
            if j >= len(text) or text[j].isspace():
                best = j
    return text[:best].strip() if best > 0 else ""


# ---------- whitespace and punctuation spacing ----------

# Python and JSON preserve these separators as literal characters. Five corpus
# rows carried U+2028 after the ordinary CR/LF cleanup, leaving an invisible
# source artefact in both the target and the trained model's output.
_UNICODE_LINE_BREAK = re.compile(r"\r\n?|[\v\f\u0085\u2028\u2029]")
_HORIZONTAL_SPACE = re.compile(r"[ \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")

# A narrower rule than "remove whitespace before punctuation": `We ... waited`
# is a real ellipsis style, and `fine :)` / `wink ;)` are text, not defects.
_SPACE_BEFORE_PUNCT = re.compile(r"(?P<left>[\w\])\"'”’])[ \t]+(?P<mark>[,;:!?])(?![-()\\/DPp])")


def normalise_spacing(text: str) -> str:
    """Canonicalise whitespace without flattening paragraph structure."""
    text = _UNICODE_LINE_BREAK.sub("\n", text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    return _SPACE_BEFORE_PUNCT.sub(r"\g<left>\g<mark>", text)


# ---------- sentence-boundary spacing ----------

# The BARE join demands a capital followed by a lowercase letter, and that is
# not an oversight: measured on the 87,716-row screened build, `[.!?][a-z]` with
# no space fires on 207 targets and almost none of them are sentence boundaries
# -- `a.m.`, `s.o.`, `4chan.net`, `2145 a.d.`, plus chat-log transcripts. The
# capital is the only thing separating a welded boundary from an abbreviation.
#
# A QUOTE-ADJACENT join carries that evidence in the glyph: no decimal, domain
# or abbreviation contains a double quote, so `couldn't."jae sighed` is
# unambiguous. Single quotes are excluded for the mirror-image reason --
# `U.A.'s Staff` is a possessive initialism, which is 15 of the 15 rows.
_SENTENCE_JOIN = re.compile(r"(?<=[A-Za-z0-9\]\)])([.!?])(?=[A-Z][a-z])")
_QUOTED_SENTENCE_JOIN = re.compile(r"(?<=[A-Za-z0-9\]\)])([.!?])([\"“”])(?=[A-Za-z])")
# Two adjacent quote glyphs after terminal punctuation are not ambiguous: the
# first closes one utterance and the second opens the next.
_BACK_TO_BACK_DIALOGUE = re.compile(r"(?<=[A-Za-z0-9\]\)])([.!?])([\"”])([\"“])(?=[A-Za-z])")


def restore_sentence_spacing(text: str) -> str:
    """Insert a missing space at a clear sentence boundary."""

    def quoted(match: re.Match) -> str:
        mark, quote = match.groups()
        if quote == "“":  # unambiguous opening glyph
            return mark + " " + quote
        if quote == "”":  # unambiguous closing glyph
            return mark + quote + " "
        # Straight quotes are symmetric. Odd parity before the punctuation means
        # this quote closes a span; even parity means it opens one.
        prefix = text[: match.start()]
        inside = len(re.findall(r'(?<!\\)"', prefix)) % 2 == 1
        return mark + quote + " " if inside else mark + " " + quote

    text = _BACK_TO_BACK_DIALOGUE.sub(r"\1\2 \3", text)
    text = _QUOTED_SENTENCE_JOIN.sub(quoted, text)
    return _SENTENCE_JOIN.sub(r"\1 ", text)


# ---------- lost paragraph breaks ----------

# The AO3 scrape does not merely drop the space between two utterances, it drops
# the PARAGRAPH BREAK, and the archive's own format is one paragraph per
# speaker. The discriminator is free, and it is the space itself: an exchange
# the author really did write into one paragraph is already close-space-open in
# the raw text (3,878 occurrences), a lost break is the tight form (73,224). The
# mark class is wider than restore_sentence_spacing's because a line of dialogue
# ends in more than a full stop; the comma is excluded because `,"` promises a
# dialogue tag, so it is not a line end.
_LOST_PARAGRAPH = re.compile(r"(?<=[A-Za-z0-9\]\)])([.!?…—–-])([\"”])([\"“])(?=[A-Za-z])")


def split_lost_paragraphs(text: str) -> str:
    """Tight close-quote/open-quote weld -> the paragraph break it used to be.

    Runs BEFORE restore_sentence_spacing, whose back-to-back rule would
    otherwise settle the same weld as a space. What survives to that rule is
    what this one declined, so the two do not overlap.
    """

    def cut(match: re.Match) -> str:
        mark, close, opening = match.groups()
        if close == "“" or opening == "”":
            return match.group(0)  # both glyphs face the wrong way
        # A straight quote is symmetric, so its role is its parity: the nth
        # quote of a line opens when n is even. Counting from the line start
        # keeps one unbalanced quote from inverting every verdict after it.
        head = text.rfind("\n", 0, match.start()) + 1
        before = len(re.findall(r'(?<!\\)"', text[head : match.start() + 1]))
        if close == '"' and before % 2 == 0:
            return match.group(0)  # even: this glyph OPENS a span
        if opening == '"' and (before + (close == '"')) % 2 == 1:
            return match.group(0)  # odd: it CLOSES one, so no new turn
        return f"{mark}{close}\n{opening}"

    return _LOST_PARAGRAPH.sub(cut, text)


def finish(text: str, stopped: bool) -> str:
    """The cleanup every rewrite gets before it is called an answer.

    `stopped` is whether the model ended the paragraph itself. A generation that
    did not ran out of budget rather than finishing, and its tail is half a
    sentence.
    """
    text = text.strip()
    if not stopped:
        text = trim_to_sentence(text) or text
    return restore_sentence_spacing(split_lost_paragraphs(normalise_spacing(text))).strip()
