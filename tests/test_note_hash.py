"""The canonical note hash (#351, design D3).

`canonical_note_text` must reproduce, byte for byte, the text the indexer's
previous text-mode read produced, so the refactor changes no stored
`content_hash` (a changed hash would re-embed the vault). The reference
implementation is kept here verbatim.
"""
import hashlib
import io
import os

import pytest

from src.services import indexer
from src.services.note_hash import (
    CanonicalNoteHasher,
    canonical_note_hash,
    canonical_note_text,
    canonical_note_text_strict,
)


def _previous_text_mode(data: bytes) -> str:
    """`read_note_at` before #351: text mode, strict UTF-8, NULs removed."""
    with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="strict") as h:
        text = h.read()
    return text.replace("\x00", "")


CASES = [
    b"plain\nlf\n",
    b"crlf\r\nline\r\n",
    b"lone\rcr\r",
    b"mixed\r\n\r\r\n\n\r",
    b"nul\x00inside\x00",
    b"\r\x00\n",
    b"\xef\xbb\xbfbom first\n",
    "unicode   sep é\r\n".encode("utf-8"),
    b"",
    b"\r",
    b"x" * 9000 + b"\r\n" + b"y" * 9000,
]


@pytest.mark.parametrize("data", CASES, ids=range(len(CASES)))
def test_the_canonical_text_is_the_previous_text_mode_read(data):
    expected = _previous_text_mode(data)
    assert canonical_note_text(data) == expected
    assert canonical_note_hash(data) == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    # The indexer's own hash of that text is the same function.
    assert indexer._content_hash(expected) == canonical_note_hash(data)


@pytest.mark.parametrize("data", [b"\xff\xfe", b"ok\xc3", b"\xe9t\xe9"])
def test_invalid_utf8_is_none_and_strict_raises(data):
    with pytest.raises(UnicodeDecodeError):
        _previous_text_mode(data)
    assert canonical_note_text(data) is None
    assert canonical_note_hash(data) is None
    with pytest.raises(UnicodeDecodeError):
        canonical_note_text_strict(data)


@pytest.mark.parametrize("data", CASES + [b"\xff"], ids=range(len(CASES) + 1))
@pytest.mark.parametrize("size", [1, 2, 3, 7, 4096])
def test_the_streaming_hasher_matches_at_every_split(data, size):
    hasher = CanonicalNoteHasher()
    for i in range(0, len(data), size):
        hasher.update(data[i:i + size])
    assert hasher.hexdigest() == canonical_note_hash(data)


def test_read_note_at_returns_the_canonical_text(tmp_path):
    for i, data in enumerate(CASES):
        (tmp_path / f"n{i}.md").write_bytes(data)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, data in enumerate(CASES):
            text, st = indexer.read_note_at(fd, f"n{i}.md")
            assert text == _previous_text_mode(data)
            assert st.st_size == len(data)
        (tmp_path / "bad.md").write_bytes(b"\xff")
        with pytest.raises(UnicodeDecodeError):
            indexer.read_note_at(fd, "bad.md")
    finally:
        os.close(fd)
