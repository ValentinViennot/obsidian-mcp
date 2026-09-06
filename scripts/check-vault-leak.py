#!/usr/bin/env python3
"""Fail if private vault content has leaked into this public repository.

This fork is public. The vault it serves is not. Folder names, note titles and
path formats are all vault content, and they leak easily — into a README
example, a test fixture written from a real note, or a docstring where someone
pasted a real path while debugging.

Two design decisions make this check usable rather than noisy:

**The denylist is built at runtime from the live vault**, whose path comes from
the environment. A denylist *of vault content* would itself be vault content, so
committing one causes the exact leak it prevents. Nothing sensitive is written
down here.

**Only our own changes are scanned** — the diff against ``upstream/main``, plus
files that do not exist upstream. Upstream's README saying "memory" is not our
leak, and scanning the whole tree drowns real findings in hundreds of
coincidences. What we are actually policing is what *this fork* adds.

Matching is deliberately restricted to **distinctive** strings: multi-word note
titles and non-generic folder names. A vault contains notes called "TODO" and
folders called "daily"; flagging those would train everyone to ignore the check,
which is worse than not having it.

Usage::

    VAULT_LEAK_CHECK_PATH=/path/to/vault python scripts/check-vault-leak.py

With no vault path set it exits 0 with a notice, so it is a no-op for
contributors who do not have the vault — which is everyone but its owner.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

#: A distinctive note title is multi-word and long. Single dictionary words are
#: not evidence of a leak, they are evidence of English.
MIN_TITLE_LEN = 15

#: Folder names are short, so they qualify by being unusual rather than long:
#: either multi-word, or long enough not to collide with ordinary vocabulary.
MIN_DIR_LEN = 8

GENERIC_DIRS = {
    "archive", "archives", "assets", "attachments", "build", "config", "daily",
    "data", "docs", "documents", "drafts", "examples", "images", "inbox",
    "index", "journal", "library", "media", "meetings", "memory", "misc",
    "monthly", "notes", "people", "personal", "pictures", "private", "projects",
    "public", "random", "reading", "resources", "reviews", "scripts", "src",
    "static", "templates", "temp", "tmp", "vendor", "weekly", "work",
}

SKIP_PATHS = {"scripts/check-vault-leak.py"}


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _upstream_ref(repo: Path) -> str | None:
    for ref in ("upstream/main", "origin/upstream-main"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True
        )
        if probe.returncode == 0:
            return ref
    return None


def our_added_lines(repo: Path) -> list[tuple[str, int, str]]:
    """Every line this fork adds on top of upstream, as (path, lineno, text)."""
    ref = _upstream_ref(repo)
    added: list[tuple[str, int, str]] = []

    if ref:
        diff = _git(["diff", "-U0", f"{ref}...HEAD"], repo)
        path = ""
        new_line = 0
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                path = line[6:]
            elif line.startswith("@@"):
                match = re.search(r"\+(\d+)", line)
                new_line = int(match.group(1)) if match else 0
            elif line.startswith("+") and not line.startswith("+++"):
                added.append((path, new_line, line[1:]))
                new_line += 1
    else:
        # No upstream remote configured: fall back to scanning tracked files, so
        # the check still does something rather than silently passing.
        for rel in _git(["ls-files"], repo).splitlines():
            try:
                text = (repo / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                added.append((rel, i, line))

    # Uncommitted work counts too — the point is to catch a leak before it is
    # committed, not after.
    for rel in _git(["diff", "--name-only", "HEAD"], repo).splitlines():
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            added.append((rel, i, line))

    return [(p, n, t) for p, n, t in added if p and p not in SKIP_PATHS]


def build_denylist(vault: Path) -> tuple[set[str], set[str]]:
    titles: set[str] = set()
    dirs: set[str] = set()

    for path in vault.rglob("*"):
        name = path.name
        if name.startswith("."):
            continue
        rel = path.relative_to(vault)
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        if path.is_dir():
            distinctive = " " in name or len(name) >= MIN_DIR_LEN
            if distinctive and name.casefold() not in GENERIC_DIRS:
                dirs.add(name)
        elif path.suffix.lower() == ".md":
            stem = path.stem
            if " " in stem and len(stem) >= MIN_TITLE_LEN:
                titles.add(stem)

    return titles, dirs


def scan(
    lines: list[tuple[str, int, str]], titles: set[str], dirs: set[str]
) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    dir_patterns = {
        d: re.compile(rf"(?<![\w-]){re.escape(d)}(?![\w-])", re.IGNORECASE) for d in dirs
    }
    title_lower = {t.casefold(): t for t in titles}

    for path, line_no, text in lines:
        lowered = text.casefold()
        for needle, original in title_lower.items():
            if needle in lowered:
                hits.append((path, line_no, f"note title: {original!r}"))
        for name, pattern in dir_patterns.items():
            if pattern.search(text):
                hits.append((path, line_no, f"vault folder: {name!r}"))
    return hits


def main() -> int:
    vault_env = os.environ.get("VAULT_LEAK_CHECK_PATH")
    if not vault_env:
        print(
            "check-vault-leak: VAULT_LEAK_CHECK_PATH not set, skipping.",
            file=sys.stderr,
        )
        return 0

    vault = Path(vault_env).expanduser()
    if not vault.is_dir():
        print(f"check-vault-leak: not a directory: {vault}", file=sys.stderr)
        return 0

    repo = Path(_git(["rev-parse", "--show-toplevel"], Path.cwd()).strip())
    titles, dirs = build_denylist(vault)
    hits = sorted(set(scan(our_added_lines(repo), titles, dirs)))

    if not hits:
        print(
            f"check-vault-leak: clean ({len(titles)} distinctive titles, "
            f"{len(dirs)} folder names checked against this fork's own changes)"
        )
        return 0

    print("check-vault-leak: PRIVATE VAULT CONTENT IN A PUBLIC REPO\n", file=sys.stderr)
    for path, line_no, what in hits:
        print(f"  {path}:{line_no}: {what}", file=sys.stderr)
    print(
        "\nRemove it. If a match is genuinely coincidental, add the path to "
        "SKIP_PATHS with a comment — never add the vault string itself here.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
