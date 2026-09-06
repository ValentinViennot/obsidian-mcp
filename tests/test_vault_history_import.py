"""Tests for the pre-git vault history importer.

The importer's job is to turn weak evidence (filenames, frontmatter, filesystem
timestamps) into a git history that is *honest about its own uncertainty*. The
risky part is not the git plumbing, it is the date extraction: a wrong rule
silently produces a confident-looking history full of invented dates. So these
tests concentrate on the parsing precedence, the ambiguous cases, and the
provenance labelling.

Every fixture here is synthetic. Nothing in this file is derived from a real
vault.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "vault_history_import.py"
_spec = importlib.util.spec_from_file_location("vault_history_import", _MODULE_PATH)
vhi = importlib.util.module_from_spec(_spec)
assert _spec.loader
# Registered before exec: @dataclass resolves its own module through
# sys.modules, and blows up with an opaque AttributeError if it is absent.
sys.modules["vault_history_import"] = vhi
_spec.loader.exec_module(vhi)


# ---------------------------------------------------------------------------
# Filename dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected,source",
    [
        ("2024-03-17 Some Note", (2024, 3, 17), "filename_iso"),
        ("2024-03-17-Sunday", (2024, 3, 17), "filename_iso"),
        ("2024-03 Monthly Thing", (2024, 3, 15), "filename_month"),
        ("240317 Some Note", (2024, 3, 17), "filename_ymd"),
        ("20240317 Some Note", (2024, 3, 17), "filename_ymd"),
        ("2403 Some Note", (2024, 3, 15), "filename_month"),
        ("Note about 2024-03-17 inside", (2024, 3, 17), "filename_iso"),
    ],
)
def test_filename_dates_are_extracted(stem, expected, source):
    got = vhi.date_from_filename(stem)
    assert got is not None, f"expected a date from {stem!r}"
    dt, got_source = got
    assert (dt.year, dt.month, dt.day) == expected
    assert got_source == source


def test_a_bare_year_is_not_read_as_a_month_code():
    """`2025` is a year, not YY=20/MM=25 — month 25 does not exist.

    This is the single most dangerous ambiguity in the four-digit family: read
    naively it produces either a crash or a wildly wrong date, and it appears
    in real vaults alongside genuine YYMM prefixes like `2512`.
    """
    assert vhi.date_from_filename("2025 Some Planning Note") is None


def test_a_real_yymm_prefix_still_parses():
    got = vhi.date_from_filename("2512 Some Note")
    assert got is not None
    dt, source = got
    assert (dt.year, dt.month) == (2025, 12)
    assert source == "filename_month"


def test_month_precision_anchors_mid_month():
    """Anchoring on the 1st would bias every month-precision note to month start."""
    dt, _ = vhi.date_from_filename("2024-06 Something")
    assert dt.day == 15


def test_undated_names_yield_nothing():
    assert vhi.date_from_filename("Just A Regular Note Title") is None


def test_an_impossible_date_is_rejected_rather_than_raising():
    assert vhi.date_from_filename("2024-13-45 Nonsense") is None


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def test_frontmatter_date_is_read(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("---\ndate: 2023-07-04\ntags: [a]\n---\n\nBody text.\n")
    got = vhi.date_from_frontmatter(note)
    assert got is not None
    dt, source = got
    assert (dt.year, dt.month, dt.day) == (2023, 7, 4)
    assert source == "frontmatter"


def test_frontmatter_is_ignored_when_absent(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Heading\n\nNo frontmatter here, but a date 2023-07-04.\n")
    assert vhi.date_from_frontmatter(note) is None


def test_frontmatter_reader_does_not_read_the_body(tmp_path):
    """A date deep in the body must not be mistaken for frontmatter.

    Also matters for privacy: this tool runs over private notes and has no
    reason to pull their content into memory.
    """
    note = tmp_path / "note.md"
    note.write_text("---\ntitle: x\n---\n" + "\n".join(["filler"] * 200) + "\ndate: 1999-01-01\n")
    assert vhi.date_from_frontmatter(note) is None


# ---------------------------------------------------------------------------
# Precedence and provenance
# ---------------------------------------------------------------------------


def test_filename_beats_frontmatter_and_filesystem(tmp_path):
    note = tmp_path / "2022-02-02 Note.md"
    note.write_text("---\ndate: 2024-04-04\n---\n")
    entry = vhi.resolve_date(note, "2022-02-02 Note.md")
    assert entry.source == "filename_iso"
    assert entry.when.year == 2022
    assert entry.observed is True
    assert entry.provenance == "observed"


def test_filesystem_fallback_is_marked_reconstructed(tmp_path):
    note = tmp_path / "Undated Note.md"
    note.write_text("no frontmatter\n")
    entry = vhi.resolve_date(note, "Undated Note.md")
    assert entry.source in {"birthtime", "mtime"}
    assert entry.observed is False
    assert entry.provenance == "reconstructed"


def test_a_future_filename_date_falls_back_to_the_filesystem(tmp_path):
    """A note named for next year is a planning note, not one written then.

    Trusting it would put HEAD in the future and make every subsequent real
    commit look out of order.
    """
    future = datetime.now(tz=timezone.utc) + timedelta(days=400)
    note = tmp_path / f"{future.year}-{future.month:02d}-01 Planning.md"
    note.write_text("plan\n")
    entry = vhi.resolve_date(note, note.name)
    assert entry.source in {"birthtime", "mtime"}
    assert entry.when <= datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Cluster spreading
# ---------------------------------------------------------------------------


def _entry(name: str, when: datetime, source: str) -> vhi.Entry:
    return vhi.Entry(relpath=name, abspath=Path(name), when=when, source=source)


def test_a_bulk_write_cluster_is_spread_across_its_day():
    """A sync client stamps many files with one second; that is not an edit session."""
    base = datetime(2024, 10, 8, 18, 31, 33, tzinfo=timezone.utc)
    entries = [_entry(f"n{i}.md", base, "birthtime") for i in range(20)]
    spread = vhi.spread_clusters(entries)
    assert spread == 20
    assert len({e.when for e in entries}) == 20, "timestamps should now be distinct"
    assert all(e.spread_from == base for e in entries)
    assert all(e.when >= base for e in entries)


def test_clusters_are_detected_at_second_precision():
    """Filesystem stamps carry microseconds; comparing them would find nothing.

    This regressed once: grouping on the full-precision datetime found zero
    clusters on a corpus that demonstrably had a 57-file one.
    """
    base = datetime(2024, 10, 8, 18, 31, 33, tzinfo=timezone.utc)
    entries = [
        _entry(f"n{i}.md", base.replace(microsecond=i * 1000), "birthtime")
        for i in range(10)
    ]
    assert vhi.spread_clusters(entries) == 10


def test_observed_dates_are_never_spread():
    """Several notes legitimately share a filename date; that is real, leave it."""
    base = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    entries = [_entry(f"n{i}.md", base, "filename_iso") for i in range(20)]
    assert vhi.spread_clusters(entries) == 0
    assert all(e.spread_from is None for e in entries)


def test_a_small_group_is_left_alone():
    base = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    entries = [_entry(f"n{i}.md", base, "mtime") for i in range(2)]
    assert vhi.spread_clusters(entries) == 0


# ---------------------------------------------------------------------------
# Commit messages
# ---------------------------------------------------------------------------


def test_reconstructed_commits_carry_an_explicit_warning():
    entry = _entry("n.md", datetime(2024, 1, 1, tzinfo=timezone.utc), "mtime")
    message = vhi.commit_message(entry)
    assert "reconstructed" in message
    assert "INFERRED" in message
    assert "Do not quote it" in message


def test_observed_commits_do_not_carry_the_warning():
    entry = _entry("n.md", datetime(2024, 1, 1, tzinfo=timezone.utc), "filename_iso")
    message = vhi.commit_message(entry)
    assert "observed" in message
    assert "INFERRED" not in message


def test_commit_messages_never_contain_note_content():
    """The vault repo is private, but content still has no business in a message."""
    entry = _entry("some/path/note.md", datetime(2024, 1, 1, tzinfo=timezone.utc), "mtime")
    message = vhi.commit_message(entry)
    assert "some/path/note.md" in message
    assert len(message.splitlines()[0]) <= 72


def test_a_spread_entry_says_so():
    entry = _entry("n.md", datetime(2024, 1, 1, tzinfo=timezone.utc), "mtime")
    entry.spread_from = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert "bulk-write cluster" in vhi.commit_message(entry)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def test_end_to_end_import_produces_per_note_birth_dates(tmp_path):
    """The whole point: one commit per file, backdated, so --follow finds a birth date."""
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "2021-01-05 Alpha.md").write_text("# Alpha\n")
    (vault / "2022-06-09 Beta.md").write_text("# Beta\n")
    (vault / "sub" / "2023-11-30 Gamma.md").write_text("# Gamma\n")

    work = tmp_path / "work"
    stats = vhi.build(
        vault=vault,
        work=work,
        bare=None,
        author="Test Author",
        email="test@example.invalid",
        tag="v0-test-import",
        dry_run=False,
        limit=None,
    )

    assert stats["files"] == 3
    assert stats["observed"] == 3

    birth = _git(
        [
            "log",
            "--diff-filter=A",
            "--follow",
            "--format=%ad",
            "--date=short",
            "--",
            "2021-01-05 Alpha.md",
        ],
        cwd=work,
    ).strip()
    assert birth == "2021-01-05"

    # Chronological order, so history reads forwards.
    dates = _git(["log", "--reverse", "--format=%ad", "--date=short"], cwd=work).split()
    note_dates = [d for d in dates if d.startswith(("2021", "2022", "2023"))]
    assert note_dates == sorted(note_dates)

    assert "v0-test-import" in _git(["tag", "-l"], cwd=work)


def test_the_boundary_tag_and_manifest_exist(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2021-01-05 Alpha.md").write_text("# Alpha\n")

    work = tmp_path / "work"
    vhi.build(
        vault=vault,
        work=work,
        bare=None,
        author="Test Author",
        email="test@example.invalid",
        tag="v0-test-import",
        dry_run=False,
        limit=None,
    )
    assert (work / ".vault-import-provenance.json").exists()
    head_body = _git(["log", "-1", "--format=%B"], cwd=work)
    assert "reconstructed" in head_body
    assert "real history" in head_body


def test_the_source_vault_is_never_modified(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "2021-01-05 Alpha.md"
    note.write_text("# Alpha\n")
    before = {p: p.stat().st_mtime_ns for p in vault.rglob("*")}

    vhi.build(
        vault=vault,
        work=tmp_path / "work",
        bare=None,
        author="Test Author",
        email="test@example.invalid",
        tag="v0-test-import",
        dry_run=False,
        limit=None,
    )

    after = {p: p.stat().st_mtime_ns for p in vault.rglob("*")}
    assert before == after
    assert not (vault / ".git").exists()


def test_dry_run_creates_nothing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2021-01-05 Alpha.md").write_text("# Alpha\n")
    work = tmp_path / "work"

    stats = vhi.build(
        vault=vault,
        work=work,
        bare=None,
        author="Test Author",
        email="test@example.invalid",
        tag="v0-test-import",
        dry_run=True,
        limit=None,
    )
    assert stats["files"] == 1
    assert not work.exists()
