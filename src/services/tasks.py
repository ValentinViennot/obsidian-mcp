"""Checkbox task extraction from note bodies (#350, design D2/D3).

One pure, synchronous function, `extract_tasks_bounded`, turns a note's
post-frontmatter body into the rows the index pass stores in `note_tasks`.
It never evaluates a Tasks-plugin or Dataview query block: those are fenced
text, masked like any other code.

## The grammar

1. The body is masked with the shared code-masking grammar
   (`mask_code(..., context=BODY)`), so fenced and inline code are spaces of
   equal length and offsets agree between the masked and original text.
2. Lines are split under the universal-newline rule (`\\r\\n`, `\\n`, `\\r`).
3. A line is a task when, at its start, the **masked** line matches
   `_CHECKBOX_RE`: optional blockquote/callout prefixes, any horizontal
   indentation, a bullet (`-`, `*`, `+`) or ordered marker (1–9 digits then
   `.` or `)`), horizontal whitespace, and a bracketed one-code-point status
   (U+0020, or any non-whitespace character but `[` and `]`) followed by
   horizontal whitespace or the end of the line. Possessive quantifiers only.
4. **The #218 rule for tasks**: the original and masked text must be equal
   from the line start through the closing `]`; otherwise masking decided the
   marker and the line is not a task.
5. Text is the original line after `]` and its following horizontal
   whitespace, trailing whitespace removed, cut at `TASK_TEXT_MAX_CHARS`.
6. Fields come from the **masked** slice of the same span, last occurrence of
   each family wins; an invalid date is NULL and never an error. Tags use the
   shared inline-tag rule (`vault.INLINE_TAG_RE`): longer than
   `TASK_TAG_MAX_CHARS` is dropped, the first `TASK_MAX_TAGS` distinct are
   kept in document order, and `tags_cut` records either drop.
7. The heading path is the stack of column-zero ATX headings
   (`vault._ATX_HEADING_RE`) above the task in the masked body.
8. At most `MAX_TASKS_PER_NOTE` tasks, in document order; the bound reports
   `truncated=True`.

**Any change to this grammar — or to any of the extraction caps — is a
`CURRENT_EXTRACTION_VERSION` bump** in `src/services/indexer.py`, the same rule
the fence and link grammars carry: a stored row is re-derived only when its
content hash or its extraction version changes.

## Composition rule for per-note derivations (design D4)

The index pass derives `note_links` and `note_tasks` per changed note. A
per-note derivation, and any third one added next to these (the
`change-feed` change, #351):

1. is keyed by `notes_metadata.id` with `ON DELETE CASCADE`;
2. is a pure function of the buffered body and the extraction version;
3. runs after the link rebuild in the same transaction, for the same changed
   list;
4. records a missing buffered body or id in the shared `skips` /
   `skipped_paths` lists;
5. never reads a file;
6. **isolates its own failure**: a per-note savepoint, one retry, then an
   owner-visible failure bit on the note's row — never the quarantine and
   never a pass failure.

A third derivation is one more call after `_update_tasks_for_changed`.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from src.config import (
    MAX_TASKS_PER_NOTE,
    TASK_MAX_TAGS,
    TASK_TAG_MAX_CHARS,
    TASK_TEXT_MAX_CHARS,
)
from src.services.links import BODY, mask_code
from src.services.vault import _ATX_HEADING_RE, INLINE_TAG_RE

#: The extraction version that introduced task rows. A row stamped below it
#: has not been scanned for tasks yet; `list_tasks` counts such rows in every
#: header rather than reporting "no tasks" for them (design D5).
TASKS_SINCE_EXTRACTION_VERSION = 5

STATE_OPEN = "open"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATES = (STATE_OPEN, STATE_DONE, STATE_CANCELLED)
PRIORITIES = ("highest", "high", "medium", "low", "lowest")

HEADING_PART_MAX_CHARS = 200
HEADING_PATH_MAX_CHARS = 512
HEADING_SEPARATOR = " > "

_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")

# Anchored by `.match(text, pos, endpos)` at each line start, so no `^`.
# Possessive throughout: no class can be re-entered by backtracking.
_CHECKBOX_RE = re.compile(
    r"(?:[ \t]*+>)*+[ \t]*+(?:[-*+]|[0-9]{1,9}[.)])[ \t]++"
    r"\[(?P<s> |[^\s\[\]])\](?=[ \t]|$)"
)
_FOLLOWING_SPACE_RE = re.compile(r"[ \t]*+")

_DATE_TAIL = r"️?(?:[ \t]*+(?P<y>[0-9]{4})-(?P<m>[0-9]{2})-(?P<d>[0-9]{2})(?![0-9]))?"
_DUE_RE = re.compile("[\U0001F4C5\U0001F4C6\U0001F5D3]" + _DATE_TAIL)
_SCHEDULED_RE = re.compile("[⏳⌛]" + _DATE_TAIL)
_START_RE = re.compile("\U0001F6EB" + _DATE_TAIL)
_DONE_RE = re.compile("✅" + _DATE_TAIL)
_PRIORITY_RE = re.compile("(?P<p>[\U0001F53A⏫\U0001F53C\U0001F53D⏬])️?")
_PRIORITY_NAMES = {
    "\U0001F53A": "highest",
    "⏫": "high",
    "\U0001F53C": "medium",
    "\U0001F53D": "low",
    "⏬": "lowest",
}


@dataclass(frozen=True)
class ExtractedTask:
    """One checkbox list item, as `note_tasks` stores it."""

    line: int
    status_char: str
    state: str
    text: str
    text_cut: bool
    due: datetime.date | None
    scheduled: datetime.date | None
    start: datetime.date | None
    done_on: datetime.date | None
    priority: str | None
    tags: list[str] | None
    tags_cut: bool
    heading_path: str | None


def state_for(status_char: str) -> str:
    """Design D2: `x`/`X` done, `-` cancelled, every other marker open."""
    if status_char in ("x", "X"):
        return STATE_DONE
    if status_char == "-":
        return STATE_CANCELLED
    return STATE_OPEN


def _last_date(pattern: re.Pattern, masked: str, start: int, end: int):
    last = None
    for last in pattern.finditer(masked, start, end):
        pass
    if last is None or last.group("y") is None:
        return None
    try:
        return datetime.date(int(last.group("y")), int(last.group("m")), int(last.group("d")))
    except ValueError:
        return None


def _priority(masked: str, start: int, end: int) -> str | None:
    last = None
    for last in _PRIORITY_RE.finditer(masked, start, end):
        pass
    return None if last is None else _PRIORITY_NAMES[last.group("p")]


def _tags(masked_slice: str) -> tuple[list[str] | None, bool]:
    kept: list[str] = []
    seen: set[str] = set()
    cut = False
    for match in INLINE_TAG_RE.finditer(masked_slice):
        tag = match.group(1)
        if len(tag) > TASK_TAG_MAX_CHARS:
            cut = True
            continue
        if tag in seen:
            continue
        if len(kept) >= TASK_MAX_TAGS:
            cut = True
            continue
        seen.add(tag)
        kept.append(tag)
    return (kept or None), cut


def _heading_path(stack: list[tuple[int, str]]) -> str | None:
    if not stack:
        return None
    path = HEADING_SEPARATOR.join(text[:HEADING_PART_MAX_CHARS] for _, text in stack)
    return path[:HEADING_PATH_MAX_CHARS]


def _lines(text: str):
    """`(start, content_end)` of every line, universal-newline rule."""
    pos = 0
    for brk in _LINE_BREAK_RE.finditer(text):
        yield pos, brk.start()
        pos = brk.end()
    yield pos, len(text)


def extract_tasks_bounded(
    body: str, *, line_offset: int, max_tasks: int = MAX_TASKS_PER_NOTE
) -> tuple[list[ExtractedTask], bool]:
    """The tasks of a post-frontmatter `body`, and whether `max_tasks` bit.

    `line_offset` is the number of line terminators in the frontmatter block,
    so `line` counts from the file's first line. `body` is NUL-free by the
    indexer's decode rule (#308); a NUL is a caller bug and raises rather than
    being repaired.
    """
    if "\x00" in body:
        raise ValueError("extract_tasks_bounded: body contains U+0000")
    masked = mask_code(body, context=BODY)
    if len(masked) != len(body):  # the masking invariant; never expected
        raise ValueError("extract_tasks_bounded: masking changed the length")

    headings = [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _ATX_HEADING_RE.finditer(masked)
    ]
    heading_index = 0
    stack: list[tuple[int, str]] = []

    tasks: list[ExtractedTask] = []
    for number, (start, end) in enumerate(_lines(body), start=1):
        while heading_index < len(headings) and headings[heading_index][0] < start:
            _, depth, text = headings[heading_index]
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, text))
            heading_index += 1

        m = _CHECKBOX_RE.match(masked, start, end)
        if m is None:
            continue
        marker_end = m.end()
        if body[start:marker_end] != masked[start:marker_end]:
            continue  # the #218 rule: masking decided the marker
        if len(tasks) >= max_tasks:
            return tasks, True

        text_start = _FOLLOWING_SPACE_RE.match(body, marker_end, end).end()
        full_text = body[text_start:end].rstrip()
        text_cut = len(full_text) > TASK_TEXT_MAX_CHARS
        tags, tags_cut = _tags(masked[text_start:end])
        status_char = m.group("s")
        tasks.append(
            ExtractedTask(
                line=number + line_offset,
                status_char=status_char,
                state=state_for(status_char),
                text=full_text[:TASK_TEXT_MAX_CHARS],
                text_cut=text_cut,
                due=_last_date(_DUE_RE, masked, text_start, end),
                scheduled=_last_date(_SCHEDULED_RE, masked, text_start, end),
                start=_last_date(_START_RE, masked, text_start, end),
                done_on=_last_date(_DONE_RE, masked, text_start, end),
                priority=_priority(masked, text_start, end),
                tags=tags,
                tags_cut=tags_cut,
                heading_path=_heading_path(stack),
            )
        )
    return tasks, False


def body_line_offset(frontmatter_block: str) -> int:
    """Line terminators in the frontmatter block, universal-newline rule."""
    return len(_LINE_BREAK_RE.findall(frontmatter_block))


def frontmatter_line_offset(raw: str, body: str) -> int:
    """`body_line_offset` of the block `parse_frontmatter` stripped from `raw`.

    The parser returns the body as a suffix of the raw text (only the block and
    its one closing terminator are removed), so the block is the prefix. A body
    that is not a suffix — never expected — counts from line 1.
    """
    if len(body) >= len(raw) or not raw.endswith(body):
        return 0
    return body_line_offset(raw[: len(raw) - len(body)])
