"""`modified_at` comes from git when the vault is a git repository.

**The bug this module pins.** `notes_metadata.modified_at` was the file's
`st_mtime`. That is a real edit time only while the vault is a directory
somebody edits in place; under git it is not. `git clone` and `git checkout`
stamp every file with the moment of the checkout, so a freshly deployed server
reported ONE identical timestamp for all 1,222 notes — and `get_recent`, whose
entire job is ordering by recency, returned an arbitrary slice in an arbitrary
order with no error anywhere to say so. It was found by an agent noticing the
dates looked wrong, not by anything in this suite.

Everything here runs against **synthetic repositories in a tmp dir**, never a
real vault, with `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` pinned so every date
assertion is exact rather than "recent".

Setup convention follows `tests/test_git_history_tools.py`: minimal env
defaults and a chdir away from any `.env` BEFORE importing.
"""

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

from src.services import git_history, indexer  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="reading commit times needs a git executable"
)

OLD = "2021-04-01T10:00:00+00:00"
MID = "2022-08-15T14:30:00+00:00"
NEW = "2023-12-25T09:05:00+00:00"


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def _git(root: Path, *args: str, env: dict | None = None) -> str:
    """Run git in `root` under a scrubbed environment.

    Hermetic: the developer's own `user.name`, `init.defaultBranch` and any
    `GIT_*` in their shell would otherwise decide what these assertions see.
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    if env:
        base.update(env)
    done = subprocess.run(
        [shutil.which("git"), "-c", "commit.gpgsign=false", *args],
        cwd=str(root),
        env=base,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout


def _commit(root: Path, message: str, when: str) -> None:
    _git(
        root,
        "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.invalid",
        "commit", "-q", "-m", message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A three-commit vault: one note untouched since the import, one edited
    later, one added last. Any implementation that reported a single time for
    all three — which is exactly what the checkout mtime did — fails here.
    """
    root = tmp_path / "vault"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    _write(root, "Ancient.md", "# Ancient\n")
    _write(root, "Revised.md", "# Revised\n")
    _git(root, "add", "-A")
    _commit(root, "import", OLD)

    _write(root, "Revised.md", "# Revised\n\nsecond thoughts\n")
    _git(root, "add", "-A")
    _commit(root, "revise", MID)

    _write(root, "Projects/Fresh.md", "# Fresh\n")
    _git(root, "add", "-A")
    _commit(root, "add fresh", NEW)
    return root


# ── the map itself ───────────────────────────────────────────────────────────


def test_each_note_gets_its_own_last_commit_time(repo):
    times, truncated = git_history.last_commit_times(git_history.resolve_repo(repo))
    assert truncated is False
    assert times["Ancient.md"] == _epoch(OLD)
    assert times["Revised.md"] == _epoch(MID)  # last write wins, not the birth
    assert times["Projects/Fresh.md"] == _epoch(NEW)


def test_checkout_mtimes_are_all_identical_and_the_map_is_not(repo, tmp_path):
    """The regression, demonstrated rather than asserted from memory.

    A fresh clone is what the production server has. Its mtimes carry no
    information at all; the history in the very same directory carries three
    distinct dates.
    """
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))

    # Not exact equality — a checkout writes its files microseconds apart, and
    # asserting float identity would be testing the disk rather than the
    # claim. The claim is that the *spread* carries no information: every note
    # is stamped within the same instant of the checkout, whatever its age.
    mtimes = [p.stat().st_mtime for p in clone.rglob("*.md")]
    assert len(mtimes) == 3
    assert max(mtimes) - min(mtimes) < 5, "a checkout stamps one instant on everything"

    times, _ = git_history.last_commit_times(git_history.resolve_repo(clone))
    spans = [times[p] for p in ("Ancient.md", "Revised.md", "Projects/Fresh.md")]
    assert len(set(spans)) == 3
    # …and they span years, in the very same directory.
    assert max(spans) - min(spans) > 365 * 24 * 3600


def test_a_rename_dates_the_new_path(repo):
    """`--no-renames` is deliberate. With rename detection ON git reports only
    the new path against the renaming commit and the path's earlier edits land
    under a name no caller ever asks about; OFF, a rename is a delete plus an
    add and the add is what a caller holding the current path looks for."""
    _git(repo, "mv", "Ancient.md", "Archive.md")
    _git(repo, "add", "-A")
    _commit(repo, "file it away", NEW)

    times, _ = git_history.last_commit_times(git_history.resolve_repo(repo))
    assert times["Archive.md"] == _epoch(NEW)


def test_a_vanished_path_keeps_its_last_write_and_never_takes_the_deletion(repo):
    """The map is a **superset** of what is on disk, and deliberately so.

    `--diff-filter=AMR` drops deletions, so a path that was deleted — or
    renamed away, which under `--no-renames` is the same thing — stays in the
    map carrying the time it was last *written*, never the time it was
    removed. That entry is inert: the indexer looks up only paths its own walk
    found on disk, so a name that no longer exists is never asked about. What
    would be a defect is the other outcome — a deletion commit dating a note
    that a later commit restored — and that is what this pins.
    """
    _git(repo, "rm", "-q", "Ancient.md")
    _commit(repo, "drop it", NEW)

    times, _ = git_history.last_commit_times(git_history.resolve_repo(repo))
    assert times["Ancient.md"] == _epoch(OLD)
    assert times["Ancient.md"] != _epoch(NEW)


def test_awkward_filenames_survive_the_nul_framing(repo):
    """`-z` plus `core.quotepath=false` is what makes this parse. Under git's
    default quoting these come back C-escaped inside double quotes and every
    one of them lands in the map under a name that is not the file's."""
    for rel in ("Notes/été & café.md", "Notes/with space.md", "Notes/quote\"mark.md"):
        _write(repo, rel, "x\n")
    _git(repo, "add", "-A")
    _commit(repo, "awkward names", NEW)

    times, _ = git_history.last_commit_times(git_history.resolve_repo(repo))
    for rel in ("Notes/été & café.md", "Notes/with space.md", "Notes/quote\"mark.md"):
        assert times[rel] == _epoch(NEW), f"missing or misparsed: {rel!r}"


def test_a_vault_inside_a_larger_repo_reports_vault_relative_paths(tmp_path):
    """The vault root need not be the repository root. git names paths from
    the toplevel, so the prefix is stripped — and a sibling directory outside
    the vault must not leak in as a path the indexer can never match."""
    root = tmp_path / "monorepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "notes/Inside.md", "# In\n")
    _write(root, "elsewhere/Outside.md", "# Out\n")
    _git(root, "add", "-A")
    _commit(root, "both", MID)

    repo = git_history.resolve_repo(root / "notes")
    assert repo.prefix == "notes/"
    times, _ = git_history.last_commit_times(repo)
    assert times == {"Inside.md": _epoch(MID)}


def test_empty_repository_yields_an_empty_map(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    times, truncated = git_history.last_commit_times(git_history.resolve_repo(root))
    assert times == {}
    assert truncated is False


def test_truncation_is_reported_and_keeps_the_newest(repo):
    """The walk is newest-first, so a cap can only ever lose the OLDEST paths.
    A partial map is worth more than none — provided the caller is told."""
    times, truncated = git_history.last_commit_times(
        git_history.resolve_repo(repo), max_bytes=40
    )
    assert truncated is True
    # Whatever survived is correct; the newest commit's path is what survives.
    assert times.get("Projects/Fresh.md") == _epoch(NEW)
    assert "Ancient.md" not in times


# ── how the indexer consumes it ──────────────────────────────────────────────


def _stat(mtime: float):
    return SimpleNamespace(st_mtime=mtime, st_size=0)


def test_modified_at_prefers_git_over_the_checkout_stamp():
    checkout = 1_900_000_000.0  # far in the future of every fixture date
    got = indexer._modified_at("Revised.md", _stat(checkout), {"Revised.md": _epoch(MID)})
    assert got == datetime.fromtimestamp(_epoch(MID), tz=timezone.utc)


def test_modified_at_falls_back_for_a_note_git_has_never_seen():
    """Not a defect: an untracked or not-yet-committed note has no commit to
    date it by, and for that window the filesystem is the only witness."""
    now = 1_700_000_000.0
    got = indexer._modified_at("Draft.md", _stat(now), {"Other.md": _epoch(OLD)})
    assert got == datetime.fromtimestamp(now, tz=timezone.utc)


async def test_git_times_are_skipped_when_the_git_vault_is_off(repo, monkeypatch):
    """A deployment that has not opted into git pays no subprocess at all."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", False)

    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("resolve_repo called with the git vault disabled")

    monkeypatch.setattr(git_history, "resolve_repo", _boom)
    assert await indexer._git_modified_times(repo, "") == {}


async def test_git_times_are_read_when_the_git_vault_is_on(repo, monkeypatch):
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)
    times = await indexer._git_modified_times(repo, "")
    assert times["Revised.md"] == _epoch(MID)


async def test_a_plain_directory_degrades_silently(tmp_path, monkeypatch):
    """Turning the switch on against a non-repository is a documented no-op
    everywhere else in this codebase; it must not fail an index pass."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert await indexer._git_modified_times(plain, "") == {}


async def test_a_git_failure_never_fails_the_pass(repo, monkeypatch):
    """Same rule `git_vault` follows for commit-on-write: a git failure
    degrades the derived data, it does not take down the pass."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)

    def _timeout(*a, **k):
        raise git_history.GitTimeout("git took too long")

    monkeypatch.setattr(git_history, "last_commit_times", _timeout)
    assert await indexer._git_modified_times(repo, "") == {}


async def test_git_missing_from_the_image_never_fails_the_pass(repo, monkeypatch):
    """The exact shape of an earlier incident: the runtime image shipped with
    no `git`, and everything that depended on it silently did nothing."""
    monkeypatch.setattr(indexer.settings, "git_vault_enabled", True)

    def _missing(*a, **k):
        raise git_history.GitMissing("git is not installed on the server")

    monkeypatch.setattr(git_history, "resolve_repo", _missing)
    assert await indexer._git_modified_times(repo, "") == {}
