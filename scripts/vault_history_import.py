#!/usr/bin/env python3
"""Reconstruct a git history for an Obsidian vault that has none.

A vault that has lived in a file-sync product has no version control, but it
does carry weak evidence of when each note was born: a date in the filename, a
date key in frontmatter, and the filesystem's own timestamps. This tool turns
that evidence into a plausible, *honestly labelled* git history so that
``git log --diff-filter=A --follow`` returns a real birth date per note and the
history tools (``note_history``, ``note_blame``, ``find_when_written``) have
something to walk.

Two passes, per the design:

1. Each file is committed **alone**, backdated to its best-known creation
   timestamp, in chronological order. Committing one file per commit is what
   makes ``--diff-filter=A --follow`` yield a true per-note birth date; a bulk
   commit would collapse every note onto one date.
2. A final commit sweeps anything left (files with no usable evidence, plus a
   provenance manifest) and marks the import boundary, which is then tagged.

The cardinal rule here is **honesty about provenance**. Every commit message
states which timestamp source was used and whether the date is *observed* or
*reconstructed*. Without that marker, an agent reading this history will assert
invented dates with unearned confidence — which is worse than having no history
at all. The boundary tag exists so that "before this tag, dates are inferred;
after it, they are real" is a single, checkable fact.

Timestamp precedence (strongest first):

===================  ==========  ==================================================
source               precision   note
===================  ==========  ==================================================
``filename_iso``     day         ``YYYY-MM-DD`` leading the basename. Strongest.
``filename_ymd``     day         ``YYMMDD`` / ``YYYYMMDD`` leading the basename.
``filename_month``   month       ``YYYY-MM`` / ``YYMM``. Anchored mid-month.
``frontmatter``      varies      a ``date:``/``created:`` key in YAML frontmatter.
``birthtime``        second      filesystem birth time, where the platform has it.
``mtime``            second      last resort; the weakest and most often mangled.
===================  ==========  ==================================================

Only ``filename_*`` and ``frontmatter`` are reported as *observed*. Filesystem
timestamps are reported as *reconstructed*, because a sync client rewrites them
in bulk: in the corpus this was built for, one single second carried 57 files.
Those bulk-write clusters are detected and **spread** across their day instead
of being stacked, so the history does not invent a 57-file commit that never
happened.

The tool never writes to the source vault. It reads, and it builds a new
repository somewhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

#: Sources we are willing to call "observed" in a commit message. Everything
#: else is "reconstructed" — see the module docstring for why that distinction
#: is load-bearing rather than cosmetic.
OBSERVED_SOURCES = frozenset(
    {"filename_iso", "filename_ymd", "filename_month", "frontmatter"}
)

#: Ordering used to break ties when several sources agree on a file; lower wins.
SOURCE_RANK = {
    "filename_iso": 0,
    "filename_ymd": 1,
    "filename_month": 2,
    "frontmatter": 3,
    "birthtime": 4,
    "mtime": 5,
}

#: A timestamp shared by at least this many files is treated as a bulk-sync
#: artefact rather than an editing session, and is spread across its day.
CLUSTER_THRESHOLD = 3


@dataclass
class Entry:
    """One file, with the evidence we have about when it was born."""

    relpath: str
    abspath: Path
    when: datetime
    source: str
    #: Set when the timestamp was moved off a detected bulk-write cluster.
    spread_from: datetime | None = None
    #: Latest modification we can see, recorded as metadata rather than
    #: fabricated into a second commit — we know *that* it changed, never *what*
    #: changed, so inventing a diff would be a lie.
    last_seen_mtime: datetime | None = None

    @property
    def observed(self) -> bool:
        return self.source in OBSERVED_SOURCES

    @property
    def provenance(self) -> str:
        return "observed" if self.observed else "reconstructed"


# --------------------------------------------------------------------------
# Date extraction
# --------------------------------------------------------------------------

_ISO_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")
_ISO_ANYWHERE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_PREFIX = re.compile(r"^(\d{4})-(\d{2})(?!\d)")
_DIGITS_PREFIX = re.compile(r"^(\d{4,8})(?:\s|_|-|$)")

_FM_DATE_KEY = re.compile(
    r"^\s*(date|created|created_at|date_created)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)


def _mk(year: int, month: int, day: int, hour: int = 12) -> datetime | None:
    """Build a UTC datetime, returning None rather than raising on nonsense."""
    try:
        return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def date_from_filename(stem: str) -> tuple[datetime, str] | None:
    """Extract a date from a note's basename.

    Several prefix conventions coexist in a vault that has been kept for years,
    and they are genuinely ambiguous with each other, so order matters. The
    four-digit case is the nastiest: ``2512`` is December 2025, but ``2025``
    cannot be (month 25 does not exist) and is far more likely a bare year.
    """
    m = _ISO_PREFIX.match(stem)
    if m:
        dt = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            return dt, "filename_iso"

    m = _MONTH_PREFIX.match(stem)
    if m:
        # Mid-month: an anchor that is wrong by at most ~15 days in either
        # direction, rather than biasing every month-precision note to the 1st.
        dt = _mk(int(m.group(1)), int(m.group(2)), 15)
        if dt:
            return dt, "filename_month"

    m = _DIGITS_PREFIX.match(stem)
    if m:
        digits = m.group(1)
        if len(digits) == 8:
            # Either YYYYMMDD or YYMMDDHH. Try the full-year reading first; it
            # is the only one that can be checked for plausibility.
            dt = _mk(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
            if dt and 1990 <= dt.year <= 2100:
                return dt, "filename_ymd"
            dt = _mk(
                2000 + int(digits[0:2]),
                int(digits[2:4]),
                int(digits[4:6]),
                min(int(digits[6:8]), 23),
            )
            if dt:
                return dt, "filename_ymd"
        elif len(digits) == 6:
            dt = _mk(2000 + int(digits[0:2]), int(digits[2:4]), int(digits[4:6]))
            if dt:
                return dt, "filename_ymd"
        elif len(digits) == 4:
            yy, mm = int(digits[0:2]), int(digits[2:4])
            if 1 <= mm <= 12 and 0 <= yy <= 99:
                dt = _mk(2000 + yy, mm, 15)
                if dt:
                    return dt, "filename_month"
            # Otherwise it is a bare year and carries no useful precision;
            # fall through and let a weaker source answer.

    m = _ISO_ANYWHERE.search(stem)
    if m:
        dt = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if dt:
            return dt, "filename_iso"

    return None


def date_from_frontmatter(path: Path) -> tuple[datetime, str] | None:
    """Read a date key out of YAML frontmatter, if there is any.

    Deliberately reads only the frontmatter block, never the body: this tool
    runs over private notes and has no reason to pull their content into memory.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return None
            for _ in range(50):
                line = fh.readline()
                if not line or line.strip() in {"---", "..."}:
                    return None
                m = _FM_DATE_KEY.match(line)
                if not m:
                    continue
                raw = m.group(2).strip().strip("\"'")
                dt = _parse_loose_date(raw)
                if dt:
                    return dt, "frontmatter"
    except (OSError, UnicodeError):
        return None
    return None


def _parse_loose_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    candidates = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%Y/%m/%d",
    )
    for fmt in candidates:
        try:
            dt = datetime.strptime(raw[: len(raw)], fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    m = _ISO_ANYWHERE.search(raw)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def date_from_stat(path: Path) -> tuple[datetime, str]:
    """Fall back to the filesystem. Always succeeds, and is always the weakest."""
    st = path.stat()
    birth = getattr(st, "st_birthtime", None)
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if birth:
        btime = datetime.fromtimestamp(birth, tz=timezone.utc)
        # A birthtime later than the mtime is impossible; when a sync client
        # produces one, trust the mtime instead.
        if btime <= mtime:
            return btime, "birthtime"
    return mtime, "mtime"


def resolve_date(path: Path, relpath: str) -> Entry:
    stem = Path(relpath).name
    best: tuple[datetime, str] | None = None

    for candidate in (date_from_filename(stem), date_from_frontmatter(path)):
        if candidate is None:
            continue
        if best is None or SOURCE_RANK[candidate[1]] < SOURCE_RANK[best[1]]:
            best = candidate

    stat_dt, stat_source = date_from_stat(path)

    # A name-derived date in the future is a planning note ("notes for next
    # quarter"), not a creation date. Committing it would put the repository's
    # HEAD in the future and make every later real commit look out of order,
    # so fall back to the filesystem for those.
    if best is not None and best[0] > _now():
        best = None

    if best is None:
        best = (stat_dt, stat_source)

    return Entry(
        relpath=relpath,
        abspath=path,
        when=best[0],
        source=best[1],
        last_seen_mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
    )


# --------------------------------------------------------------------------
# Cluster spreading
# --------------------------------------------------------------------------


def spread_clusters(entries: list[Entry], threshold: int = CLUSTER_THRESHOLD) -> int:
    """Fan bulk-write timestamp collisions across their day.

    A sync client that rewrites a directory stamps every file with the same
    second. Left alone that becomes one enormous commit at one instant, which
    reads as a real editing session and is not one. Only filesystem-derived
    timestamps are spread — a genuine shared filename date (several notes
    legitimately dated the same day) is left exactly as it is.
    """
    groups: dict[datetime, list[Entry]] = defaultdict(list)
    for entry in entries:
        if entry.source in {"birthtime", "mtime"}:
            # Group by whole seconds. Filesystem timestamps carry microsecond
            # resolution, but a bulk sync writes many files within the same
            # second with *different* microseconds — comparing the full
            # precision would find no clusters at all and silently defeat this
            # whole function.
            groups[entry.when.replace(microsecond=0)].append(entry)

    spread = 0
    for when, members in groups.items():
        if len(members) < threshold:
            continue
        members.sort(key=lambda e: e.relpath)
        # Spread across the remainder of the day in even steps, keeping the
        # original ordering stable and deterministic.
        span = timedelta(hours=12)
        step = span / max(len(members), 1)
        for index, entry in enumerate(members):
            entry.spread_from = when
            entry.when = when + step * index
            spread += 1
    return spread


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


def git(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def commit_message(entry: Entry) -> str:
    """Build a message that cannot be mistaken for a real editing session."""
    verb = "add"
    subject = f"{verb}: {entry.relpath}"
    if len(subject) > 72:
        subject = subject[:69] + "..."

    lines = [
        subject,
        "",
        f"Imported from the pre-git vault. Date provenance: {entry.provenance}.",
        f"Timestamp source: {entry.source}.",
    ]
    if entry.observed:
        lines.append(
            "This date is read from the note itself (filename or frontmatter) "
            "and is as trustworthy as the note's own naming."
        )
    else:
        lines.append(
            "This date is INFERRED from filesystem metadata, which a sync "
            "client rewrites in bulk. Treat it as approximate. Do not quote it "
            "as the moment the note was written."
        )
    if entry.spread_from is not None:
        lines.append(
            f"Original filesystem timestamp {entry.spread_from.isoformat()} was "
            "shared by a bulk-write cluster; this entry was spread within that "
            "day so the import does not fabricate a single mass edit."
        )
    if entry.last_seen_mtime and entry.last_seen_mtime > entry.when:
        lines.append(
            f"Last observed modification: {entry.last_seen_mtime.isoformat()}. "
            "The content between creation and that date is unrecoverable, so no "
            "intermediate commit is fabricated for it."
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

DEFAULT_EXCLUDES = (
    ".git",
    ".obsidian",
    ".smart-env",
    ".trash",
    ".DS_Store",
    "Icon\r",
    "Icon",
)


def scan(vault: Path, excludes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(vault):
        dirnames[:] = sorted(d for d in dirnames if d not in excludes)
        for name in sorted(filenames):
            if name in excludes:
                continue
            path = Path(root) / name
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size == 0 and not name.endswith(".md"):
                # 128 zero-byte extensionless artefacts is a sync-client
                # signature, not content worth versioning.
                continue
            found.append(path)
    return found


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build(
    vault: Path,
    work: Path,
    bare: Path | None,
    author: str,
    email: str,
    tag: str,
    dry_run: bool,
    limit: int | None,
) -> dict:
    files = scan(vault, DEFAULT_EXCLUDES)
    if limit:
        files = files[:limit]

    entries = [resolve_date(p, str(p.relative_to(vault))) for p in files]
    spread = spread_clusters(entries)
    entries.sort(key=lambda e: (e.when, e.relpath))

    by_source: dict[str, int] = defaultdict(int)
    for entry in entries:
        by_source[entry.source] += 1

    stats = {
        "files": len(entries),
        "spread_from_clusters": spread,
        "by_source": dict(sorted(by_source.items())),
        "observed": sum(1 for e in entries if e.observed),
        "reconstructed": sum(1 for e in entries if not e.observed),
        "earliest": entries[0].when.isoformat() if entries else None,
        "latest": entries[-1].when.isoformat() if entries else None,
    }

    if dry_run:
        return stats

    work.mkdir(parents=True, exist_ok=True)
    git(["init", "-q", "-b", "main"], cwd=work)
    git(["config", "user.name", author], cwd=work)
    git(["config", "user.email", email], cwd=work)

    gitignore = Path(__file__).resolve().parent.parent / "deploy" / "vault.gitignore"
    if gitignore.exists():
        (work / ".gitignore").write_bytes(gitignore.read_bytes())
        env = _commit_env(entries[0].when if entries else _now(), author, email)
        git(["add", ".gitignore"], cwd=work)
        git(
            [
                "commit",
                "-q",
                "-m",
                "chore: add vault gitignore before importing history\n\n"
                "Keeps plugin caches and workspace state out of the repository. "
                "One cache file alone can be hundreds of megabytes, and once "
                "committed it is in the history permanently.\n",
            ],
            cwd=work,
            env=env,
        )

    for index, entry in enumerate(entries, start=1):
        target = work / entry.relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.abspath.read_bytes())
        git(["add", "--", entry.relpath], cwd=work)
        # A file matched by .gitignore stages nothing; skip rather than making
        # an empty commit.
        if not git(["diff", "--cached", "--name-only"], cwd=work).strip():
            continue
        git(
            ["commit", "-q", "-m", commit_message(entry)],
            cwd=work,
            env=_commit_env(entry.when, author, email),
        )
        if index % 200 == 0:
            print(f"  … {index}/{len(entries)} committed", file=sys.stderr)

    manifest = {
        "generated_by": "scripts/vault_history_import.py",
        "note": (
            "Provenance manifest for the pre-git import. Commits before the "
            f"'{tag}' tag carry reconstructed dates as described per-commit. "
            "Commits after it are real."
        ),
        "stats": stats,
    }
    (work / ".vault-import-provenance.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    git(["add", "--", ".vault-import-provenance.json"], cwd=work)
    git(
        [
            "commit",
            "-q",
            "-m",
            "chore: close the pre-git import boundary\n\n"
            "Everything before this commit was reconstructed from filenames, "
            "frontmatter and filesystem metadata by "
            "scripts/vault_history_import.py. Dates are labelled per commit as "
            "observed or reconstructed.\n\n"
            "Everything after this commit is real history: authored by the "
            "vault owner from the desktop, or by the MCP server's agent "
            "identity on write.\n",
        ],
        cwd=work,
        env=_commit_env(_now(), author, email),
    )
    git(["tag", "-a", tag, "-m", "Boundary: reconstructed history ends here"], cwd=work)

    if bare:
        git(["init", "--bare", "-q", "-b", "main", str(bare)], cwd=work.parent)
        git(["remote", "add", "origin", str(bare)], cwd=work)
        git(["push", "-q", "--set-upstream", "origin", "main"], cwd=work)
        git(["push", "-q", "--tags", "origin"], cwd=work)

    stats["commits"] = int(git(["rev-list", "--count", "HEAD"], cwd=work).strip())
    return stats


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _commit_env(when: datetime, author: str, email: str) -> dict[str, str]:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z") or when.isoformat()
    return {
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--vault", required=True, type=Path, help="source vault (read-only)")
    parser.add_argument("--work", required=True, type=Path, help="working clone to create")
    parser.add_argument("--bare", type=Path, help="optional bare repo to push into")
    parser.add_argument("--author", default="Vault Import")
    parser.add_argument("--email", default="import@localhost")
    parser.add_argument("--tag", default="v0-drive-import")
    parser.add_argument("--dry-run", action="store_true", help="report statistics only")
    parser.add_argument("--limit", type=int, help="only process the first N files")
    args = parser.parse_args()

    if not args.vault.is_dir():
        parser.error(f"vault not found: {args.vault}")
    if not args.dry_run and args.work.exists() and any(args.work.iterdir()):
        parser.error(f"working directory is not empty: {args.work}")

    stats = build(
        vault=args.vault,
        work=args.work,
        bare=args.bare,
        author=args.author,
        email=args.email,
        tag=args.tag,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
