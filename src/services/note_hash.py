"""The canonical note hash (#351, design D3).

One definition of "the content hash of a note", shared by the indexer and by
every tool that records a change-feed event. Before it the indexer hashed the
text its text-mode read produced (universal newlines, U+0000 removed, #308 D1)
while a tool knew only the raw bytes it published, so a CRLF note or a note
holding a NUL hashed differently on the two sides and the pass could never
recognise a tool's own edit when it saw it.

- `canonical_note_text(data)` is a strict UTF-8 decode (`None` when the bytes
  are not UTF-8), then `\\r\\n` and lone `\\r` translated to `\\n` exactly as
  Python's text mode does, then every U+0000 removed. A UTF-8 BOM is kept (the
  indexer decodes with `utf-8`, not `utf-8-sig`).
- `canonical_note_hash(data)` is the SHA-256 hex of that text's UTF-8, or
  `None` for bytes that are not UTF-8 (the index will not index them, so there
  is nothing to compare with).
- `CanonicalNoteHasher` is the same function fed in chunks, for the two
  streaming publishers (`import_from_url` and `PUT /transfer/upload`).

`src/services/indexer.py`'s `read_note_at` reads bytes and derives its text
through `canonical_note_text_strict`, so `notes_metadata.content_hash` *is*
`canonical_note_hash(file bytes)`; `tests/test_note_hash.py` pins that against
the previous text-mode implementation.
"""
from __future__ import annotations

import codecs
import hashlib


def _translate(text: str) -> str:
    """Universal-newline translation, then NUL removal (the indexer's order)."""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        text = text.replace("\x00", "")
    return text


def canonical_note_text_strict(data: bytes) -> str:
    """The canonical text of `data`; raises `UnicodeDecodeError` if not UTF-8."""
    return _translate(data.decode("utf-8", errors="strict"))


def canonical_note_text(data: bytes) -> str | None:
    """The canonical text of `data`, or `None` when it is not valid UTF-8."""
    try:
        return canonical_note_text_strict(data)
    except UnicodeDecodeError:
        return None


def text_hash(text: str) -> str:
    """SHA-256 hex of an already-canonical text's UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_note_hash(data: bytes) -> str | None:
    """SHA-256 hex of `canonical_note_text(data)`, or `None` if not UTF-8."""
    text = canonical_note_text(data)
    if text is None:
        return None
    return text_hash(text)


class CanonicalNoteHasher:
    """`canonical_note_hash` over a byte stream, chunk by chunk.

    A `\\r` at the end of a chunk is held back until the next chunk shows
    whether a `\\n` follows, so a CRLF split across two chunks is translated
    once, exactly as a whole-buffer read would.
    """

    __slots__ = ("_decoder", "_digest", "_pending_cr", "_failed")

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._digest = hashlib.sha256()
        self._pending_cr = False
        self._failed = False

    def _feed_text(self, text: str, final: bool) -> None:
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        if not final and text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        if text:
            self._digest.update(_translate(text).encode("utf-8"))

    def update(self, chunk: bytes) -> None:
        if self._failed or not chunk:
            return
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._failed = True
            return
        self._feed_text(text, final=False)

    def hexdigest(self) -> str | None:
        """The hash of everything fed, or `None` if it was not UTF-8."""
        if self._failed:
            return None
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            self._failed = True
            return None
        self._feed_text(tail, final=True)
        return self._digest.hexdigest()
