"""Query class: `identifier` or `prose` (#356, design D5).

A pure function, pinned by a table-driven test. A query is `identifier` when it
has at most four whitespace tokens and at least one of:

(a) a token with one of ``_ . / : # ` `` between word characters
    (`snake_case`, `v0.9.3`, `src/main.py`), or starting with ``#`` or a
    backtick directly followed by a word character (`#356`, `` `cfg` ``), or a
    digit between word characters or next to a letter (`sha256`, `v2`,
    `2026`);
(b) a camelCase / PascalCase token, i.e. a lowercase letter followed by an
    uppercase one (`semanticSearch`, `ReadNoteResult`);
(c) a token that is an all-caps acronym of two or more letters, optionally
    followed by digits, once surrounding punctuation is stripped (`API`,
    `HNSW`, `RRF`);
(d) the whole query is a single double-quoted phrase (`"exact phrase"`).

Everything else, including the empty string, is `prose`. The report shows the
per-class counts, so a misclassifying heuristic is visible as an implausible
split; a manual judgment may set `class` explicitly, which overrides this.
"""
from __future__ import annotations

import re
import string
from typing import Literal

QueryClass = Literal["identifier", "prose"]
CLASSES: tuple[str, ...] = ("identifier", "prose")

MAX_IDENTIFIER_TOKENS = 4

_SEPARATOR_BETWEEN = re.compile(r"\w[_./:#`]\w")
_LEADING_SIGIL = re.compile(r"^[#`]\w")
_DIGIT_BETWEEN = re.compile(r"(?<=\w)\d(?=\w)")
_LETTER_DIGIT = re.compile(r"[^\W\d_]\d|\d[^\W\d_]")
_CAMEL = re.compile(r"[a-z][A-Z]")
_ACRONYM = re.compile(r"[A-Z]{2,}\d*")
_QUOTED_PHRASE = re.compile(r'"[^"]+"')


def _identifier_token(token: str) -> bool:
    if (
        _SEPARATOR_BETWEEN.search(token)
        or _LEADING_SIGIL.search(token)
        or _DIGIT_BETWEEN.search(token)
        or _LETTER_DIGIT.search(token)
        or _CAMEL.search(token)
    ):
        return True
    core = token.strip(string.punctuation)
    return bool(core) and _ACRONYM.fullmatch(core) is not None


def classify(text: str) -> QueryClass:
    """`identifier` or `prose` for one query text (D5)."""
    stripped = text.strip()
    tokens = stripped.split()
    if not tokens or len(tokens) > MAX_IDENTIFIER_TOKENS:
        return "prose"
    if _QUOTED_PHRASE.fullmatch(stripped):
        return "identifier"
    return "identifier" if any(_identifier_token(t) for t in tokens) else "prose"
