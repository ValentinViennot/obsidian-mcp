"""Git plumbing for the history tools (`note_history`, `note_blame`,
`find_when_written`).

The vault is becoming a git repository with reconstructed history, and these
three tools answer "when, and by whom, was this written?" from it. Everything
that spawns a process lives here; `src/mcp_server/tools.py` keeps the MCP
layer thin and does the rendering.

**This is the only module in the codebase that runs a subprocess**, so the
rules it follows are written down rather than implied:

* **Always an explicit argv list, never `shell=True`.** Every caller-supplied
  value — a path, the pickaxe needle — is its own list element, so nothing a
  caller sends can become a second command, a redirection or an option. The
  needle in particular is passed as the element after `-S`, and the pathspec
  after a literal `--`, so neither can be read as a flag however it begins.
* **The caller never names the repository.** `root` comes from the vault
  service's own resolution, and the relative path has already been through
  `validate_visible_path` (containment + dot-dir refusal) before it reaches
  this module. Nothing here re-implements containment, and nothing here
  accepts an absolute path.
* **Every invocation is bounded twice** — a wall-clock deadline and a byte cap
  on stdout — and the process group is killed when either is hit. An external
  process on a single-worker server is a stall for every other tenant, and its
  stdout is a tool result, which is model input.
* **The ambient environment does not decide what git does.** Every `GIT_*`
  variable is dropped (`GIT_DIR` alone would silently redirect the whole
  module at another repository) and the config layers are pinned explicitly.

Every function here is **blocking**. The tool layer calls them through
`asyncio.to_thread`; nothing in this module touches the event loop.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import (
    GIT_HISTORY_MAX_OUTPUT_BYTES,
    GIT_HISTORY_TIMEOUT_SECONDS,
)

# The name git itself documents for a list of revisions blame should look
# through rather than at — bulk reformats, mass frontmatter migrations, the
# `prettier` pass over a whole vault. Held at the vault root because that is
# the directory a vault owner edits; it is a dot-file, so the vault's own file
# tools cannot see it, which is correct — it is repository configuration, not
# a note.
IGNORE_REVS_FILENAME = ".git-blame-ignore-revs"

# Record and field separators for the `--format` templates. ASCII RS/US:
# outside every plausible commit subject, author name and path, and — unlike a
# newline or a tab — not producible by the fields themselves. `%s` is git's
# *subject*, which is a single line by construction, so the only multi-line
# span in a record is the name-status block that follows the final `%x1f`.
_RS = "\x1e"
_FS = "\x1f"

# One commit record. The trailing `%x1f` is load-bearing: with `-z
# --name-status`, git appends the NUL-separated file list after the formatted
# text, so terminating the template with a separator makes that list the last
# field rather than something glued onto the subject.
_COMMIT_FORMAT = (
    f"%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1f"
)

# `A`, `M`, `D`, `T`, `U`, `X`, `B`, and the similarity-scored `R100` / `C75`.
_STATUS_RE = re.compile(r"^[ACDMRTUXB]\d*$")

_STATUS_LABELS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type changed",
    "U": "unmerged",
    "X": "unknown",
    "B": "pairing broken",
}

# Substrings of git's own `fatal:` lines that mean "this repository has no
# commits yet" rather than "something went wrong". Matched case-insensitively
# against stderr. An empty repository is a legitimate state for a vault whose
# history is still being reconstructed, and the honest answer is an empty
# history, not an error.
_EMPTY_REPO_MARKERS = (
    "does not have any commits yet",
    "bad default revision 'head'",
    "unknown revision or path not in the working tree",
    "bad revision 'head'",
    # `git blame` on a file that exists on disk but is not in HEAD. An
    # uncommitted note is an ordinary state in a vault an agent is writing to,
    # not an error, and the answer is "no commit has authored these lines".
    "no such path",
)


class GitHistoryError(Exception):
    """Base for every refusal this module raises. Carries caller-facing prose."""


class GitMissing(GitHistoryError):
    """No `git` executable on PATH.

    A deployment decision, not a request problem: the image or host simply has
    no git. Separated from every other failure so the tool can say what an
    operator would have to change.
    """


class NotAGitRepository(GitHistoryError):
    """The vault root is not inside a git working tree.

    The expected state for a vault whose history has not been reconstructed
    yet, so the message says what the tools would need rather than reading as
    a fault.
    """


class GitTimeout(GitHistoryError):
    """One invocation outlived `GIT_HISTORY_TIMEOUT_SECONDS` and was killed."""


class GitFailed(GitHistoryError):
    """git exited non-zero for a reason this module does not classify.

    Carries the first line of git's stderr, which is the only part worth
    forwarding: the rest is usually advice aimed at an interactive shell.
    """


@dataclass(frozen=True)
class FileChange:
    """One path a commit touched, as `--name-status` reported it."""

    status: str
    #: `status` in words — "added", "modified", "renamed", …
    label: str
    #: Repository-relative path *after* the change.
    path: str
    #: Repository-relative path before it, for a rename or a copy only.
    old_path: str | None = None


@dataclass(frozen=True)
class Commit:
    """One commit, in the shape all three tools return.

    Both timestamps are exactly what git printed for `%aI` / `%cI` — strict
    ISO 8601 carrying the *original* offset, never this server's timezone.
    "You wrote this at 02:14" has to mean 02:14 where the author was.
    """

    sha: str
    short_sha: str
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committer_email: str
    committed_at: str
    subject: str
    changes: tuple[FileChange, ...] = ()

    def change_for(self, repo_path: str) -> FileChange | None:
        """This commit's change to `repo_path`, in a `--follow` log.

        `--follow` rewrites the pathspec as it walks *back* through renames,
        so a commit older than the rename reports the note under its former
        name and no exact match exists — which is precisely the commit a
        caller most wants labelled, since it is usually the one that added the
        note. The single-entry fallback covers it: `--follow` filters each
        commit's diff down to the followed file, so one entry can only be that
        file under whatever name it had then.
        """
        for change in self.changes:
            if change.path == repo_path or change.old_path == repo_path:
                return change
        if len(self.changes) == 1:
            return self.changes[0]
        return None


@dataclass(frozen=True)
class BlameLine:
    """One line of `git blame --line-porcelain` output."""

    line_no: int
    content: str
    sha: str
    short_sha: str
    author_name: str
    author_email: str
    authored_at: str
    #: The path the line came from *in that commit* — different from the note's
    #: current path whenever `-C`/`-M` attributed the line to a move or a copy.
    origin_path: str

    @property
    def uncommitted(self) -> bool:
        """True for a line that is in the working tree and in no commit.

        git spells this as an all-zero object name. It is an ordinary state in
        a vault an agent writes to, and it must not be rendered as though some
        commit authored the line.
        """
        return set(self.sha) == {"0"}


@dataclass(frozen=True)
class Repo:
    """A resolved git working tree, and where the vault sits inside it.

    `prefix` is the vault root's own path relative to `toplevel`, with a
    trailing slash (empty when the vault root *is* the repository root, which
    is the expected deployment). Every path git prints is repository-relative,
    so `prefix` is what turns one back into the vault-relative path a caller
    named.
    """

    root: Path
    toplevel: str
    prefix: str


@dataclass(frozen=True)
class HistoryResult:
    commits: tuple[Commit, ...] = ()
    #: The commit the note first appears in — its "created" date. `None` when
    #: the path has no history at all.
    birth: Commit | None = None
    #: False when the birth commit was inferred from an output git truncated,
    #: so a caller is never told a date is the creation date when it might not
    #: be.
    birth_certain: bool = True
    #: True when git's stdout hit `GIT_HISTORY_MAX_OUTPUT_BYTES`.
    truncated: bool = False


@dataclass(frozen=True)
class BlameResult:
    lines: tuple[BlameLine, ...] = ()
    #: Line range actually blamed, 1-based and inclusive.
    start_line: int = 1
    end_line: int = 1
    #: Lines in the file as git counts them.
    total_lines: int = 0
    #: True when the range was cut to `MAX_BLAME_LINES`.
    capped: bool = False
    #: The ignore-revs file's state: "absent", "applied", or "unusable".
    ignore_revs: str = "absent"


@dataclass(frozen=True)
class SearchResult:
    commits: tuple[Commit, ...] = ()
    truncated: bool = False


@dataclass
class _Output:
    stdout: str
    stderr: str
    returncode: int
    truncated: bool = False
    _killed: bool = field(default=False, repr=False)


def _git_binary() -> str:
    """The absolute path to `git`, or `GitMissing`.

    Resolved every call rather than cached: an operator installing git into a
    running container should not have to restart the server, and the lookup is
    a handful of `stat`s.
    """
    found = shutil.which("git")
    if found is None:
        raise GitMissing(
            "git is not installed on the server, so the history tools cannot "
            "run. They read the vault's git repository directly; install git "
            "in the server's image or host and they start working with no "
            "other change."
        )
    return found


def _git_env() -> dict[str, str]:
    """The environment every invocation runs under.

    Two halves. **Every `GIT_*` variable is dropped** — `GIT_DIR`,
    `GIT_WORK_TREE`, `GIT_INDEX_FILE` and `GIT_ALTERNATE_OBJECT_DIRECTORIES`
    each silently point git at a repository other than the one this module
    resolved, and an ambient one would make the tools answer about a different
    history than the vault's without any error. **Then the layers are pinned**:
    no system config, no global config, no terminal prompt, no pager, no
    opportunistic index refresh (these are read-only calls and the vault may
    be on a read-only mount), and `LC_ALL=C` so nothing parsed here depends on
    the host's locale.

    Repository-local config (`.git/config`) is deliberately *not* disabled: it
    belongs to the vault, it is what a `.mailmap` or a rename-detection tuning
    lives beside, and it is already trusted by everything else that opens this
    repository.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
    )
    return env


def _base_args(root: Path, toplevel: str | None = None) -> list[str]:
    """The `-c` overrides that precede every subcommand.

    `safe.directory` is the container case and not a theoretical one: the
    vault is bind-mounted into the image, so the repository is routinely owned
    by a different uid than the server process, and git refuses such a
    repository outright ("dubious ownership"). The global config that would
    normally carry the exemption is disabled above, so the exemption is passed
    per-call and names *only* the directories this module already resolved —
    never `*`.

    `core.quotepath=false` keeps non-ASCII paths as their own bytes instead of
    C-style escapes; `log.showSignature=false` keeps a signed repository's
    verification output from being interleaved into the records parsed here.
    """
    args = [
        "--no-pager",
        "-c",
        f"safe.directory={root}",
        "-c",
        "core.quotepath=false",
        "-c",
        "log.showSignature=false",
    ]
    if toplevel and toplevel != str(root):
        args += ["-c", f"safe.directory={toplevel}"]
    return args


def _run(
    root: Path,
    args: list[str],
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> _Output:
    """Run one git invocation under a deadline and a byte cap.

    Not `subprocess.run`. That helper reads until EOF, which is exactly the
    two failure modes worth preventing here: a pickaxe over a huge history
    holding the thread for however long git wants, and a `--line-porcelain`
    blame materialising an unbounded string. So stdout is read through
    `select` against a monotonic deadline and stopped at `max_bytes`.

    stderr goes to a temporary file rather than a second pipe. Reading two
    pipes in one loop is where this pattern usually deadlocks — git blocking
    on a full stderr buffer while this loop waits on stdout — and a file
    cannot fill.

    The child gets its own session (`start_new_session`) so a timeout or a cap
    can kill the whole **process group**: git spawns helpers, and killing only
    the leader leaves an orphan holding the pipe open.

    Both bounds default to the module constants *at call time*, not at
    definition time, so lowering either one is a supported thing to do without
    reloading the module.
    """
    timeout = GIT_HISTORY_TIMEOUT_SECONDS if timeout is None else timeout
    max_bytes = GIT_HISTORY_MAX_OUTPUT_BYTES if max_bytes is None else max_bytes
    argv = [_git_binary(), *args]
    deadline = time.monotonic() + timeout
    out = bytearray()
    truncated = False
    killed = False

    with tempfile.TemporaryFile() as err_file:
        # Explicit argv, never a shell: no element of `argv` is ever built by
        # concatenating a caller's value into a string.
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err_file,
                env=_git_env(),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            # The git binary was found, so this is the *working directory* —
            # a `VAULT_PATH` naming something that is not a readable
            # directory. An uncaught `OSError` here would leave the tool
            # layer nothing to catch and reach the agent as a protocol error
            # rather than as the in-band refusal every other misconfiguration
            # gets.
            raise GitFailed(
                "The vault root could not be opened to run git in "
                f"({exc.strerror or exc}). Check the server's vault path."
            ) from exc
        try:
            fd = proc.stdout.fileno()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_group(proc)
                    raise GitTimeout(
                        f"git did not finish within {timeout:g} seconds and was "
                        "stopped. The repository's history may be too large for "
                        "this query; narrow it with a path or a smaller limit."
                    )
                ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                room = max_bytes - len(out)
                # Strictly greater: a chunk that *exactly* fills the budget
                # may still be the end of git's output, and the next read
                # settles it. Flagging it here would report a truncation that
                # did not happen, in a response whose whole value is that its
                # caveats are true.
                if len(chunk) > room:
                    out += chunk[:room]
                    truncated = True
                    killed = True
                    _kill_group(proc)
                    break
                out += chunk
            returncode = _wait(proc, deadline)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                _kill_group(proc)
                proc.wait()

        err_file.seek(0)
        stderr = err_file.read(64 * 1024).decode("utf-8", "replace")

    return _Output(
        stdout=out.decode("utf-8", "replace"),
        stderr=stderr,
        returncode=returncode,
        truncated=truncated,
        _killed=killed,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group, tolerating an already-dead child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _wait(proc: subprocess.Popen, deadline: float) -> int:
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return proc.wait(timeout=remaining or 0.1)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.wait()
        raise GitTimeout(
            "git produced its output but did not exit; it was stopped."
        ) from None


def _classify(result: _Output) -> None:
    """Raise the typed error for a non-zero exit, or return for a clean one.

    A killed process (cap hit) exits non-zero by construction and is *not* a
    failure — the caller already knows the output was truncated.
    """
    if result.returncode == 0 or result._killed:
        return
    lowered = result.stderr.lower()
    if "not a git repository" in lowered:
        raise NotAGitRepository(_not_a_repo_message())
    if "dubious ownership" in lowered:
        raise GitFailed(
            "git refuses to read the vault's repository because it is owned by "
            "a different user than the server process ('dubious ownership'). "
            "Make the repository readable by the server's user, or add it to "
            "git's `safe.directory` for that user."
        )
    first = next((line for line in result.stderr.splitlines() if line.strip()), "")
    raise GitFailed(f"git failed: {first or 'no diagnostic output'}")


def _is_empty_history(result: _Output) -> bool:
    """True when git's complaint is 'there is nothing here yet'."""
    lowered = result.stderr.lower()
    return any(marker in lowered for marker in _EMPTY_REPO_MARKERS)


def _not_a_repo_message() -> str:
    return (
        "The vault is not a git repository, so there is no history to read. "
        "The history tools read the vault's own git repository; initialise one "
        "at the vault root (or mount one there) and they start working."
    )


def resolve_repo(root: Path) -> Repo:
    """Locate the working tree containing `root`.

    Raises `NotAGitRepository` when there is none, `GitMissing` when there is
    no git. Both are the two states an operator can act on, and both are
    reported by every tool the same way.
    """
    result = _run(root, [*_base_args(root), "rev-parse", "--show-toplevel", "--show-prefix"])
    if result.returncode != 0:
        _classify(result)
        raise NotAGitRepository(_not_a_repo_message())
    lines = result.stdout.splitlines()
    if not lines:
        raise NotAGitRepository(_not_a_repo_message())
    toplevel = lines[0].strip()
    prefix = lines[1].strip() if len(lines) > 1 else ""
    return Repo(root=root, toplevel=toplevel, prefix=prefix)


# ── Parsing ─────────────────────────────────────────────────────────────────


def _parse_name_status(blob: str) -> tuple[FileChange, ...]:
    """Parse the NUL-separated `--name-status -z` tail of one record.

    Tokens arrive as `status`, then one path — or, for `R`/`C`, two: the old
    path and the new one, in that order.
    """
    tokens = [t for t in blob.split("\0") if t not in ("", "\n")]
    changes: list[FileChange] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].lstrip("\n")
        if not _STATUS_RE.match(token):
            # Not a status word: a stray token from an output shape this
            # parser does not model. Skip it rather than mis-pair every
            # remaining path with the wrong status.
            i += 1
            continue
        letter = token[0]
        label = _STATUS_LABELS.get(letter, letter)
        if letter in ("R", "C") and i + 2 < len(tokens):
            changes.append(
                FileChange(
                    status=token,
                    label=label,
                    path=tokens[i + 2],
                    old_path=tokens[i + 1],
                )
            )
            i += 3
            continue
        if i + 1 < len(tokens):
            changes.append(
                FileChange(status=token, label=label, path=tokens[i + 1])
            )
            i += 2
            continue
        i += 1
    return tuple(changes)


def _parse_commits(stdout: str) -> tuple[Commit, ...]:
    """Parse `_COMMIT_FORMAT` records, newest first, skipping partial ones.

    A record short of its nine fields is dropped rather than padded: it can
    only come from an output the byte cap cut mid-record, and half a commit
    rendered as a fact is worse than one missing row that the truncation
    notice already accounts for.
    """
    commits: list[Commit] = []
    for chunk in stdout.split(_RS):
        if not chunk.strip("\n\0 "):
            continue
        fields = chunk.split(_FS)
        if len(fields) < 9:
            continue
        (
            sha,
            short_sha,
            author_name,
            author_email,
            authored_at,
            committer_name,
            committer_email,
            committed_at,
            subject,
        ) = fields[:9]
        tail = fields[9] if len(fields) > 9 else ""
        commits.append(
            Commit(
                sha=sha,
                short_sha=short_sha,
                author_name=author_name,
                author_email=author_email,
                authored_at=authored_at,
                committer_name=committer_name,
                committer_email=committer_email,
                committed_at=committed_at,
                subject=subject,
                changes=_parse_name_status(tail),
            )
        )
    return tuple(commits)


def _iso_from_epoch(seconds: str, tz: str) -> str:
    """Render blame's `<epoch> <+hhmm>` pair as strict ISO 8601.

    `--line-porcelain` reports author time as a UTC epoch plus the author's
    own offset, and the offset is the load-bearing half: converting to the
    server's timezone would answer "when did you write this" in a timezone the
    author was never in.

    UTC comes back as `Z`, not `+00:00`, because that is what git's own `%aI`
    prints — and the two renderings sit side by side in an agent's context
    (`note_history` uses git's, `note_blame` uses this one). One dialect, so a
    caller comparing a blame line against a history line is comparing strings
    that can actually be equal.
    """
    try:
        epoch = int(seconds)
    except (TypeError, ValueError):
        return ""
    sign = 1 if tz.startswith("+") else -1
    digits = tz[1:] if tz[:1] in "+-" else tz
    try:
        offset_minutes = int(digits[:2]) * 60 + int(digits[2:4])
    except (TypeError, ValueError):
        offset_minutes = 0
    zone = timezone(timedelta(minutes=sign * offset_minutes))
    rendered = datetime.fromtimestamp(epoch, zone).isoformat()
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _parse_blame(stdout: str) -> tuple[BlameLine, ...]:
    """Parse `--line-porcelain`: one full header block per line, then `\\t<text>`."""
    lines: list[BlameLine] = []
    header: dict[str, str] = {}
    sha = ""
    line_no = 0
    for raw in stdout.split("\n"):
        if raw.startswith("\t"):
            lines.append(
                BlameLine(
                    line_no=line_no,
                    content=raw[1:],
                    sha=sha,
                    short_sha=sha[:7],
                    author_name=header.get("author", ""),
                    author_email=header.get("author-mail", "").strip("<>"),
                    authored_at=_iso_from_epoch(
                        header.get("author-time", ""), header.get("author-tz", "+0000")
                    ),
                    origin_path=header.get("filename", ""),
                )
            )
            header = {}
            continue
        if not raw:
            continue
        parts = raw.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) == 40 and _is_hex(parts[0]):
            sha = parts[0]
            numbers = parts[1].split(" ")
            if len(numbers) >= 2 and numbers[1].isdigit():
                line_no = int(numbers[1])
            header = {}
            continue
        key, _, value = raw.partition(" ")
        header[key] = value
    return tuple(lines)


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


# ── The three queries ───────────────────────────────────────────────────────


def note_history(repo: Repo, repo_path: str, limit: int) -> HistoryResult:
    """`git log --follow` over one path, newest first, plus its birth commit.

    `--follow` is what makes the answer survive a rename, and it is also why
    the birth commit needs its own invocation: the newest `limit` commits do
    not contain the oldest one unless the note has fewer than `limit` of them,
    and "when was this created" is the question most often asked of a vault.
    """
    log = _run(
        repo.root,
        [
            *_base_args(repo.root, repo.toplevel),
            "log",
            "--follow",
            "-M",
            "--name-status",
            "-z",
            f"--format={_COMMIT_FORMAT}",
            f"--max-count={limit}",
            "--",
            repo_path,
        ],
    )
    if log.returncode != 0 and not log._killed:
        if _is_empty_history(log):
            return HistoryResult()
        _classify(log)
    commits = _parse_commits(log.stdout)
    birth, certain = _birth_commit(repo, repo_path, commits, log.truncated)
    return HistoryResult(
        commits=commits,
        birth=birth,
        birth_certain=certain,
        truncated=log.truncated,
    )


def _birth_commit(
    repo: Repo,
    repo_path: str,
    commits: tuple[Commit, ...],
    log_truncated: bool,
) -> tuple[Commit | None, bool]:
    """The commit this path first appears in, and whether that is certain.

    Asked as `--diff-filter=A`, which is both cheap (a handful of records for
    any real note) and exact. The *oldest* such record is the birth: a note
    deleted and re-created has more than one add, and only the first is its
    creation.

    The fallback exists for the shapes `--diff-filter=A` misses — a path
    present in the root commit of a repository built by an import that git
    records without an add, or a filter interaction on an unusual history. It
    walks the full follow log and takes its last record, which is only the
    birth if git was not cut off, hence the `certain` flag.
    """
    if not commits:
        return None, True
    adds = _run(
        repo.root,
        [
            *_base_args(repo.root, repo.toplevel),
            "log",
            "--follow",
            "-M",
            "--diff-filter=A",
            "--name-status",
            "-z",
            f"--format={_COMMIT_FORMAT}",
            "--",
            repo_path,
        ],
    )
    if adds.returncode == 0 or adds._killed:
        parsed = _parse_commits(adds.stdout)
        if parsed:
            # Newest first, so the last record is the earliest add.
            return parsed[-1], not adds.truncated
    if log_truncated:
        return commits[-1], False
    return commits[-1], True


def blame(
    repo: Repo,
    repo_path: str,
    *,
    start_line: int,
    end_line: int,
    total_lines: int,
    max_lines: int,
    ignore_revs_path: Path | None,
) -> BlameResult:
    """`git blame -w -M -C --line-porcelain` over one line range.

    The three flags are not decoration. `-w` ignores whitespace-only changes,
    so a re-indent does not reassign a line; `-M` follows a move within the
    file, so reordering sections does not; `-C` follows a copy between files
    in the same commit, so text split out of one note into another keeps its
    original author. On a vault whose history is largely reformats and
    reorganisations, they are most of the difference between a useful
    attribution and "everything was written by whoever last tidied it".

    `--ignore-revs-file` is applied only when the file exists. If git rejects
    it — a stale sha, a malformed line — the blame is retried *without* it and
    the result says so, because a broken ignore list is a reason to give worse
    attribution, not none.
    """
    capped = end_line - start_line + 1 > max_lines
    if capped:
        end_line = start_line + max_lines - 1

    def _invoke(ignore: Path | None) -> _Output:
        args = [
            *_base_args(repo.root, repo.toplevel),
            "blame",
            "-w",
            "-M",
            "-C",
            "--line-porcelain",
            "-L",
            f"{start_line},{end_line}",
        ]
        if ignore is not None:
            args += ["--ignore-revs-file", str(ignore)]
        # No revision argument: blame the **working tree**, not `HEAD`.
        #
        # Two reasons, and the first is a bug rather than a preference. The
        # caller resolved its line range against the file on disk — that is
        # where a `section` selector's headings are, and where `read_note`
        # would have shown them — so an uncommitted edit that added lines
        # makes `-L 1,<lines on disk>` overrun `HEAD`'s shorter version, and
        # git answers a *fatal* to a request that was correct about the file
        # it named. Second: a vault an agent is actively writing to always has
        # uncommitted lines, and reporting them as "not committed yet" (git's
        # all-zero sha) is the true answer, where blaming `HEAD` would
        # silently attribute today's text to yesterday's line numbers.
        args += ["--", repo_path]
        return _run(repo.root, args)

    ignore_state = "absent"
    result = _invoke(ignore_revs_path)
    if ignore_revs_path is not None:
        if result.returncode == 0 or result._killed:
            ignore_state = "applied"
        else:
            ignore_state = "unusable"
            result = _invoke(None)
    if result.returncode != 0 and not result._killed:
        if _is_empty_history(result):
            return BlameResult(
                start_line=start_line,
                end_line=end_line,
                total_lines=total_lines,
                capped=capped,
                ignore_revs=ignore_state,
            )
        _classify(result)
    return BlameResult(
        lines=_parse_blame(result.stdout),
        start_line=start_line,
        end_line=end_line,
        total_lines=total_lines,
        capped=capped or result.truncated,
        ignore_revs=ignore_state,
    )


def search_history(
    repo: Repo,
    text: str,
    *,
    limit: int,
    repo_path: str | None = None,
    regex: bool = False,
) -> SearchResult:
    """`git log -S <text>` — the pickaxe. Commits that *introduced* the string.

    `-S` selects commits where the **number of occurrences** of the string
    changed, which is the difference between this and a grep over history: a
    commit that merely edits a line elsewhere in a file already containing the
    text is not a match, and the first commit returned walking backwards is
    the one that wrote it.

    `--follow` is added only when a single path scopes the search, because git
    accepts it for exactly one pathspec. Unscoped, the search is limited to
    the vault's own subtree (`-- .`, resolved against `cwd`) so a vault living
    inside a larger repository never answers about files outside it.
    """
    args = [
        *_base_args(repo.root, repo.toplevel),
        "log",
        "-M",
        "-S",
        text,
        "--name-status",
        "-z",
        f"--format={_COMMIT_FORMAT}",
        f"--max-count={limit}",
    ]
    if regex:
        args.append("--pickaxe-regex")
    if repo_path is not None:
        args += ["--follow", "--", repo_path]
    else:
        args += ["--", "."]
    result = _run(repo.root, args)
    if result.returncode != 0 and not result._killed:
        if _is_empty_history(result):
            return SearchResult()
        _classify(result)
    return SearchResult(
        commits=_parse_commits(result.stdout), truncated=result.truncated
    )


def last_commit_times(
    repo: Repo,
    *,
    timeout: float | None = None,
    max_bytes: int | None = None,
) -> tuple[dict[str, int], bool]:
    """`{vault-relative path: epoch seconds of the commit that last wrote it}`.

    **Why the indexer needs this at all.** `notes_metadata.modified_at` was the
    file's `st_mtime`, which is a true edit time only while the vault is a
    directory somebody edits in place. Under git it is not: `git clone` and
    `git checkout` stamp *every* file with the moment of the checkout, so a
    freshly-deployed server reports one identical timestamp for the whole
    vault and `get_recent` — whose entire job is ordering by recency — returns
    an arbitrary slice in an arbitrary order, with no error anywhere to say
    so. The vault's real dates are in its history; this reads them.

    **One invocation for the whole vault, not one per note.** A per-file
    `git log -1` over a few thousand notes is a few thousand process spawns on
    every pass. This walks the history once — newest commit first — and takes
    each path's *first* appearance, which is by definition the last commit
    that wrote it. On the maintainer's vault (1,314 commits, 1,222 notes) that
    is ~70 KB of output in about half a second.

    `--diff-filter=AMR` drops deletions, so a path that was deleted and never
    restored contributes nothing and simply is not in the map. `--no-renames`
    is deliberate rather than incidental: with rename detection on, a rename
    reports only the *new* path against the commit that renamed it, and the
    path's own earlier edits then land under a name the caller never asks
    about. Off, a rename is a delete plus an add, and the add is what a
    caller holding the current path is looking for.

    **The second element of the tuple is `True` when the answer is partial** —
    git's output hit the byte cap and was cut off mid-history. Because the
    walk is newest-first, truncation only ever loses the *oldest* paths, so
    what survives is correct and the caller falls back to `st_mtime` for the
    rest. Returning that flag rather than raising is the point: a partial map
    is worth strictly more than no map, and the caller can say which it got.

    **The map is a superset of what is on disk.** Dropping deletions means a
    path that was deleted, or renamed away, keeps the time it was last
    *written* rather than vanishing. Those entries are inert for the intended
    caller — the indexer looks up only paths its own walk found — and the
    alternative is worse: a deletion commit would date a note that a later
    commit restored.

    Callers must treat a path's absence as "no git answer", not as "never
    modified". A note written but not yet committed — the window between an
    MCP write and its commit, or between an out-of-band edit and the reconcile
    sweep — is absent or carries its previous commit's time, and `st_mtime` is
    the truer answer for exactly that window.
    """
    args = [
        *_base_args(repo.root, repo.toplevel),
        "log",
        "-z",
        # `%x01` is the record separator. It cannot occur in a path (git
        # forbids control bytes in tracked names) and `-z` means git emits the
        # names raw, so nothing here needs unquoting.
        "--format=format:%x01%ct",
        "--name-only",
        "--diff-filter=AMR",
        "--no-renames",
        "HEAD",
        # Confine the walk to the caller's vault root. Names still come back
        # relative to the repository toplevel, so `prefix` is stripped below.
        "--",
        ".",
    ]
    result = _run(repo.root, args, timeout=timeout, max_bytes=max_bytes)
    if result.returncode != 0 and not result._killed:
        if _is_empty_history(result):
            return {}, False
        _classify(result)

    prefix = repo.prefix
    times: dict[str, int] = {}
    for record in result.stdout.split("\x01"):
        if not record:
            continue
        head, _, tail = record.partition("\n")
        try:
            when = int(head.strip())
        except ValueError:
            # A record cut in half by the byte cap. Newest-first means every
            # complete record before it is already banked.
            continue
        for name in tail.split("\0"):
            if not name:
                continue
            if prefix:
                if not name.startswith(prefix):
                    continue
                name = name[len(prefix) :]
            # First occurrence wins: the walk is newest-first.
            times.setdefault(name, when)
    return times, result.truncated


def ignore_revs_file(root: Path) -> Path | None:
    """The vault's `.git-blame-ignore-revs`, when it is a real file.

    A symlink is refused rather than followed, matching every other leaf this
    server opens: the name is a constant, so a symlink under it is somebody
    aiming the read somewhere the vault owner did not name. `lstat` (not
    `exists`) is what makes that check meaningful.
    """
    candidate = root / IGNORE_REVS_FILENAME
    try:
        st = os.lstat(candidate)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return candidate
