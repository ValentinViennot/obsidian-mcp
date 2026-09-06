"""Commit-on-write: the vault as a git working clone, with attributable history.

## What this is for

The vault is a git working clone on the server *and* on the owner's desktop.
Both write to it. Six months later the question an operator actually asks is
**"when did this paragraph appear, and was it me or an agent?"** — and the only
tool that answers it is `git blame` / `git log -S`, which answers it only if the
commit exists and carries an identity.

So every MCP write produces its own commit, immediately, naming the tool and the
calling principal. A slower sweep (`deploy/vault-reconcile.sh`) catches
everything else: the desktop's pushes, a file dropped in by hand, the indexer's
housekeeping. Immediate commits are what make the history *readable*; the sweep
is what makes it *complete*.

## Four rules, and why each is not negotiable

**1. A git failure may never fail or roll back a write.** By the time anything
here runs, `_atomic_write_at` has already published bytes to disk — durably,
through an `fsync`ed staging inode. Nothing in this module can un-publish that,
and pretending otherwise (raising into the tool, "rolling back" by reverting the
file) would turn a bookkeeping problem into the destructive write this codebase
spends most of its effort preventing. Every failure here is logged at WARNING
and swallowed; the two things git reports as failures that are not — an ignored
path, and a path neither on disk nor tracked — are DEBUG, because a message that
fires on correct behaviour trains an operator to ignore the channel that also
carries "the audit trail has a gap in it". `commit_paths` and `commit_recorded`
each answer a bool that says whether a commit was made; **nothing on the write
path reads it**, and the tests are its only real consumer.

**2. Global git config is never touched.** The same repository is cloned on the
owner's desktop, where commits must stay attributable to the human. A
`git config --global user.name "…"` on the server would be invisible there —
but a `git config user.name` in the *repo* would not be, and neither would a
committer identity leaking into a desktop commit through a shared hook. Identity
travels in the environment of the one `git commit` invocation
(`GIT_AUTHOR_*` / `GIT_COMMITTER_*`), which is process-scoped and cannot escape.

**3. The index is never left half-staged.** `git add` then `git commit` is two
steps, and the second can fail. A staged-but-uncommitted path would be swept
into whatever the *sweep* commits next, under the sweep's identity and message —
silently mis-attributing an agent write to "out-of-band edit". So a failed
commit unstages exactly the paths this call staged, and nothing else.

**4. One git process at a time, in this process.** Concurrent `git add`s against
one index corrupt it. The lock is a process-local `asyncio.Lock`: this server
already documents `--workers 1` as part of its contract (the `/mcp` rate
limiter's token buckets live in worker memory — see
`docs/architecture/rate-limits.md`), so a process-local lock is exactly as
strong as the guarantees the server already makes, and no stronger. **A second
uvicorn worker would break this the same way it breaks the rate limits**, and
the mitigation is the same one: don't run two. A cross-process file lock would
not help anyway — the desktop clone and a human's shell are other processes
entirely, which is why `_clear_stale_index_lock` exists.

## What is NOT here

No push, no pull, no fetch, no remote of any kind. A network operation on the
write path would put an unbounded, unavailable-at-any-moment dependency between
an agent's `edit_note` and its answer. Synchronisation is the sweep's job, on a
timer, where a failure is an alert rather than a stalled tool call.

## Accepted gaps, written down rather than discovered

* **A tool body that raises *after* publishing produces no commit.** `_tracked`
  flushes on the success path only; an exception unwinds past it. The state is
  already anomalous — bytes on disk and a `tool_exception` row beside them —
  and awaiting a commit inside an exception path (or a `finally`, which
  cancellation also runs) would add a second, worse failure mode to the first.
  The sweep commits it within a tick, under "changed outside the MCP server",
  which is the honest label for a write nobody can attribute.
* **`PUT /transfer/upload` does not commit.** It publishes vault bytes without
  being a tool call, runs under a capability rather than inside a `_tracked`
  body, and has no accumulator to record into. The sweep attributes it, which
  is also more honest: the party that streamed the bytes is not the principal
  that chose the path.
* **A second uvicorn worker breaks the lock**, exactly as it breaks the rate
  limiter. `--workers 1` is already the contract; this adds nothing new to it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from contextvars import ContextVar

from src.config import settings

logger = logging.getLogger(__name__)


# ── Bounds ──────────────────────────────────────────────────────────────────

#: Longest commit *subject* this module will produce. Git's own conventions put
#: the soft limit at 50 and the hard one at 72; `git log --oneline` truncates
#: past the terminal width regardless. A vault path can be far longer than that
#: (`MAX_PATH_CHARS` is 1,024), so the subject is elided rather than allowed to
#: become a wrapped paragraph that no `--oneline` view can read.
MAX_SUBJECT_CHARS = 72

#: Longest sanitised principal label in a trailer. `usage_logs.actor_label` is
#: bounded at 255 for the same value; a trailer is read by a human in
#: `git log`, so it is bounded tighter.
MAX_PRINCIPAL_CHARS = 80

#: Paths listed in the commit body before the list is summarised instead. Only
#: `move_note(rewrite_links=True)` can exceed it, and a body holding several
#: hundred backlink sources is noise in every view that renders it — the diff
#: names them all, exactly, and is the authority.
MAX_BODY_PATHS = 20

#: How old `.git/index.lock` must be before this module treats it as the
#: leftover of a crashed git rather than a live one. Generously above any commit
#: this module can produce (each is bounded by `GIT_COMMIT_TIMEOUT_SECONDS`,
#: default 15) *and* above a plausible manual `git` invocation in a shell on the
#: same clone, because removing a live git's lock corrupts the index this
#: whole module exists to keep intact. Five minutes is the deliberate bias
#: towards "leave it alone": the cost of waiting is a few uncommitted writes the
#: sweep picks up, and the cost of being wrong is a broken repository.
STALE_INDEX_LOCK_SECONDS = 300


# ── The per-call accumulator ────────────────────────────────────────────────
#
# Why a ContextVar rather than a parameter on every write helper: the tool name
# and the calling principal are resolved by `_tracked` (`src/mcp_server/tools.py`)
# and the *paths* are known only inside the tool body, sometimes at several
# publication sites in one call (`move_note` publishes a rename plus one write
# per rewritten backlink source). A ContextVar lets each publication site record
# what it published — a list append, which cannot fail and cannot block — and
# lets the decorator that already owns the identity perform exactly one commit
# per tool call, after the body has completed.
#
# Per-task, like `_current_tool_name` and the timing holder beside it: each MCP
# tool call runs in its own task, so two concurrent calls cannot see each
# other's paths. `_tracked` owns the lifecycle (`begin` at the top, `clear` in
# the same `finally` as the rest), so an early return or an exception can never
# leak a path into the next call on the same task.
_pending: ContextVar[list[tuple[str, str]] | None] = ContextVar(
    "_git_vault_pending", default=None
)


def begin():
    """Open a fresh accumulator for one tool call. Returns the reset token."""
    return _pending.set([])


def clear(token) -> None:
    """Close the accumulator `begin()` opened. Never raises."""
    try:
        _pending.reset(token)
    except ValueError:  # pragma: no cover - a token from another context
        _pending.set(None)


def record_write(vault_root, rel_path) -> None:
    """Note that `rel_path` under `vault_root` was just published.

    Called from a tool body **immediately after a publication has succeeded**,
    and from nowhere else: a path recorded before the write would produce a
    commit for a write that a later refusal prevented, which is a lie in the
    one place this change exists to make trustworthy.

    Cheap and infallible by construction — a list append behind two guards, no
    I/O, no lock, no `await`. It runs on the event loop inside the critical
    path of every write tool, so it must cost nothing measurable and it must
    not be able to raise: a bookkeeping helper that threw would fail a write
    that already stood.
    """
    if not settings.git_vault_enabled or not settings.git_commit_on_write:
        return
    pending = _pending.get()
    if pending is None:
        # Outside a `_tracked` call — an internal caller, a test, the indexer.
        # Nothing will flush it, so recording would only grow a list nobody
        # reads.
        return
    try:
        pending.append((str(vault_root), str(rel_path)))
    except Exception:  # noqa: BLE001 - bookkeeping may not fail a write
        logger.warning(
            "Could not record %s for a vault commit; the write stands and the "
            "reconcile sweep will pick it up.",
            rel_path,
        )


def pending_paths() -> list[tuple[str, str]]:
    """What this call has recorded so far. `[]` outside a tool call."""
    return list(_pending.get() or ())


# ── Repository detection ────────────────────────────────────────────────────


def is_git_vault(vault_root) -> bool:
    """Is `vault_root` the root of a git working tree?

    Deliberately a filesystem test and not `git rev-parse`: this runs on the
    write path of every mutating tool, and a subprocess per write to answer a
    question whose answer changes approximately never is not a trade worth
    making. `.git` is accepted as a directory (an ordinary clone) or as a file
    (a `git worktree` / submodule gitlink), because both are working trees.

    **A vault that merely sits *inside* a repository is not one.** `git` itself
    would happily walk up and find the enclosing `.git`, and committing a
    parent repository the operator never pointed at this server is precisely
    the surprise a self-hosted tool must not spring. The `-C <root>` invocation
    below would do that walk, so this guard is what stops it.
    """
    try:
        return os.path.exists(os.path.join(str(vault_root), ".git"))
    except (OSError, ValueError, TypeError):  # pragma: no cover - defensive
        return False


def git_executable() -> str | None:
    """The `git` binary, or `None` when there is none on this machine.

    Resolved per call rather than cached: an image that gains git after the
    process started is a legitimate (if unusual) deployment, and the failure
    mode of a cached `None` is a server that silently never commits again until
    it is restarted.
    """
    return shutil.which("git")


# ── Message construction ────────────────────────────────────────────────────


def _sanitise(text: str, limit: int) -> str:
    """One line of `text`, control characters removed, bounded to `limit`.

    **Both inputs this touches are caller-influenced.** A vault path may
    contain any byte a POSIX filename may, newline included; and a principal
    label is either an API-key name typed into the panel or an *OAuth client
    name*, which a dynamically-registered client chooses for itself. Either one
    carrying a newline could otherwise close the subject and open a forged
    trailer block — a commit whose `Principal:` line says whatever the client
    wanted it to say, in the exact record an operator consults to find out who
    wrote something.

    So: every C0/C1 control character and every Unicode line separator becomes
    a space, runs of whitespace collapse, and the result is elided with `…`
    rather than silently cut (a truncated path that still looks like a path is
    worse than one that says it was truncated).
    """
    cleaned = "".join(
        " " if (ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F) else ch
        for ch in str(text)
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"


#: The verb each tool's subject reads with. `git log --oneline` is a list of
#: subjects, so the subject has to say what happened without being unfolded.
_TOOL_VERBS = {
    "create_note": "create",
    "edit_note": "update",
    "move_note": "move",
    "set_frontmatter": "update frontmatter of",
    "delete_note": "delete",
    "write_file": "write",
    "delete_file": "delete",
}


def build_message(tool: str, rel_paths, principal: str | None) -> str:
    """The commit message for one tool call.

    ```
    mcp(edit_note): update Projects/Roadmap.md

    Tool: edit_note
    Principal: laptop-key
    ```

    Parseable in both directions: `git log --grep '^mcp(edit_note)'` finds every
    call of one tool, and the trailers are real git trailers, so
    `git log --format='%(trailers:key=Principal,valueonly)'` and
    `git interpret-trailers --parse` read them without a bespoke parser.

    **Paths, never content.** The vault's own repository is private, so a path
    is fine and is the single most useful thing the subject can carry; note
    *text* is not, in any form — not a diff excerpt, not a title read out of
    frontmatter, not a snippet of what changed. The diff already holds the
    content for anyone with the repository, and a commit message is copied into
    mirrors, hooks and notification payloads that the diff is not.
    """
    verb = _TOOL_VERBS.get(tool, "write")
    paths = [_sanitise(p, MAX_SUBJECT_CHARS) for p in rel_paths]
    # Whether the subject stands in for the paths rather than naming them. Only
    # then does the body list them: repeating two paths the subject already
    # spells out is noise in every view that renders the whole message.
    summarised = True
    if not paths:
        what = "vault"
    elif len(paths) == 1:
        what = paths[0]
        summarised = False
    elif tool == "move_note" and len(paths) == 2:
        what = f"{paths[0]} → {paths[1]}"
        summarised = False
    else:
        what = f"{len(paths)} paths"
    subject = _sanitise(f"mcp({tool}): {verb} {what}", MAX_SUBJECT_CHARS)

    lines = [subject, ""]
    # `subject.endswith("…")` matters: two long move paths are named in the
    # subject in principle and elided out of it in practice, and a commit whose
    # message names neither end of a move is the one thing this must not
    # produce.
    if len(rel_paths) > 1 and (summarised or subject.endswith("…")):
        shown = list(rel_paths)[:MAX_BODY_PATHS]
        lines.extend(f"  {_sanitise(p, 200)}" for p in shown)
        if len(rel_paths) > len(shown):
            lines.append(f"  … and {len(rel_paths) - len(shown)} more")
        lines.append("")
    # The trailer block, last and separated by a blank line — that is what makes
    # these trailers rather than body text `git interpret-trailers` ignores.
    lines.append(f"Tool: {_sanitise(tool, 64)}")
    lines.append(f"Principal: {_sanitise(principal or 'unknown', MAX_PRINCIPAL_CHARS)}")
    return "\n".join(lines) + "\n"


# ── The git invocation ──────────────────────────────────────────────────────


def _pathspec(rel_path: str) -> str:
    """`rel_path` as a pathspec that cannot be read as pathspec magic.

    Git honours magic prefixes (`:(top)`, `:!`, `:/`) **after** the `--`
    separator, so `--` alone does not make an arbitrary filename safe to pass
    as a pathspec: a note legitimately named `:(exclude)notes.md` would
    otherwise turn a commit of one file into a commit of everything but it.
    `:(literal)` disables magic and wildcards for the remainder, and leaves the
    path interpreted relative to the working directory, which `-C <root>` has
    already set to the vault root.
    """
    return f":(literal){rel_path}"


def _git_env(author_name: str, author_email: str) -> dict:
    """The environment for one git invocation.

    Identity is set **here and nowhere else**. Not `git config user.name`, which
    writes `.git/config` — a file that is not shared with the desktop clone but
    that an operator inspecting *either* clone would then have to reason about —
    and emphatically not `--global`, which would relabel every commit the human
    makes on a machine that happens to also run this server.

    The rest is about making git non-interactive and unopinionated:

    * `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS`, `SSH_ASKPASS` — nothing here talks
      to a remote, but a stray credential prompt on a server with no terminal is
      a process that blocks until the timeout rather than failing.
    * `GIT_OPTIONAL_LOCKS=0` — suppresses the opportunistic index refresh git
      performs for its own convenience; it is the one lock acquisition on this
      path that has nothing to do with what we asked for.
    * `LC_ALL=C` — so the strings this module matches on ("nothing to commit")
      are the strings git produces, whatever the host locale is.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    # An `EDITOR` inherited from the operator's shell would open an editor on a
    # commit that supplies `-m`. It cannot happen with `-m`, and it is removed
    # anyway: a server process should not carry an interactive editor at all.
    for name in ("EDITOR", "VISUAL", "GIT_EDITOR"):
        env.pop(name, None)
    return env


def _run(git: str, root: str, args: list[str], env: dict, timeout: float):
    """One git invocation. Returns the `CompletedProcess`; never raises.

    `subprocess.run` with an explicit argv list and **no shell** — the arguments
    include vault paths and a caller-influenced commit message, and a shell
    between this process and git would make every one of them a quoting
    question. Timeout-bounded because git can block indefinitely on a lock, an
    unresponsive filesystem or a credential prompt, and this runs inside a
    worker thread that a stalled git would occupy for ever.

    `-c safe.directory=<root>` is passed on the command line rather than
    configured: containers routinely bind-mount a vault owned by a different uid
    than the one the server runs as, and git's "dubious ownership" refusal would
    otherwise make commit-on-write fail on exactly the deployment this feature
    is for. Command-line scope means it applies to this invocation and leaves no
    trace in any config file.
    """
    argv = [git, "-c", f"safe.directory={root}", "-C", root, *args]
    try:
        return subprocess.run(  # noqa: S603 - explicit argv, never shell=True
            argv,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "git %s in the vault timed out after %.1fs; the write stands and "
            "the reconcile sweep will pick it up.",
            args[0] if args else "?",
            timeout,
            extra={"reason": "timeout"},
        )
        return None
    except OSError as exc:
        logger.warning(
            "Could not run git %s in the vault (%s); the write stands and the "
            "reconcile sweep will pick it up.",
            args[0] if args else "?",
            exc,
            extra={"reason": "spawn_failed", "error_type": type(exc).__name__},
        )
        return None


def _clear_stale_index_lock(root: str) -> None:
    """Remove `.git/index.lock` when it can only be a crashed git's leftover.

    A git killed by this module's own timeout — or by the container's OOM
    killer, or by a `docker compose down` mid-commit — leaves the lock behind,
    and every subsequent commit then fails identically for ever. That is a
    silent, permanent end to commit-on-write, recoverable only by an operator
    who happens to read the logs.

    Two conditions, and both are deliberately conservative, because deleting a
    **live** git's lock corrupts the index this module exists to protect:

    * we hold the process-local commit lock, so it is not one of ours; and
    * the lock is older than `STALE_INDEX_LOCK_SECONDS`, which is far longer
      than any commit this module can make.

    A human's `git rebase` in a shell on this clone can still hold a lock for
    longer than that. It is the accepted residual: five minutes is well past
    any interactive command's normal life, the loss if we are wrong is one
    operator command, and the alternative — never clearing it — is the
    permanent outage above.

    Only attempted when `.git` is a real directory. For a `git worktree` or a
    submodule gitlink, `.git` is a *file* naming the real git directory
    elsewhere, and guessing at that path is how a tool deletes something it did
    not understand.
    """
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        return
    lock = os.path.join(git_dir, "index.lock")
    try:
        age = time.time() - os.stat(lock).st_mtime
    except OSError:
        return  # No lock, or we cannot see it. Either way, nothing to do.
    if age < STALE_INDEX_LOCK_SECONDS:
        return
    try:
        os.unlink(lock)
    except OSError as exc:
        logger.warning(
            "Found a %.0fs-old .git/index.lock in the vault and could not "
            "remove it (%s); commits will keep failing until it is cleared.",
            age,
            exc,
            extra={"reason": "stale_lock", "error_type": type(exc).__name__},
        )
        return
    logger.warning(
        "Removed a stale .git/index.lock (%.0fs old) from the vault. A git "
        "process was killed mid-commit; every commit since then has failed.",
        age,
        extra={"reason": "stale_lock_cleared"},
    )


#: What `git commit` says when the staged paths turn out to hold no change.
#: Not a failure: `write_file` may republish identical bytes, and `move_note`
#: plans a rewrite for a source whose links resolve to the same text.
_NOTHING_TO_COMMIT = ("nothing to commit", "no changes added to commit")

#: What `git add` says for the two non-failures it reports as exit 1 and 128.
#:
#: **An ignored path.** `deploy/vault.gitignore` deliberately excludes things an
#: agent can legitimately write — `.obsidian/workspace.json`, anything under
#: `.trash/`, a plugin cache directory — and `git add` on an *explicitly named*
#: ignored path is an error (exit 1) rather than the silent skip it is for a
#: bare `add -A`. Warning about it would put one line in the log for every such
#: write, saying only that the ignore file did what it says.
#:
#: **A path that is neither on disk nor tracked** (exit 128, "did not match any
#: files"). The reachable case is a delete of a file whose *creation* never got
#: committed — git was broken then and is working now — so there is genuinely
#: nothing to record, and the sweep already caught up with the rest.
#:
#: Both are logged at DEBUG and neither is a warning: a message that fires on
#: correct behaviour trains an operator to ignore the channel that also carries
#: "the audit trail has a gap in it".
_ADD_NON_FAILURES = (
    "ignored by one of your .gitignore files",
    "did not match any files",
)


def commit_paths(
    paths,
    message: str,
    author_name: str,
    author_email: str,
    vault_root,
) -> bool:
    """Stage exactly `paths` and commit them. Blocking; **never raises**.

    Returns True when a commit was created, False for every other outcome —
    not a git repository, no git binary, nothing changed, or a failure. The
    return value is a signal for logging and for tests; **no write path depends
    on it**, which is rule 1 at the top of this module.

    `paths` are vault-relative. They are staged with `git add -A --`, so a path
    that no longer exists stages as a deletion — which is what makes
    `delete_note` and `delete_file` produce a commit through the same helper as
    every write, with no separate deletion path to keep in step.

    The commit is `--only`, so it carries exactly these paths whatever else may
    be in the index. That matters on a clone a human also uses: an operator who
    staged something by hand in a shell must not have it swept into a commit
    signed by the agent identity and captioned with a tool name.

    On a failed commit the staging is undone for these paths and these paths
    alone (`git reset -q --`). Rule 3: a path left staged would be swept into
    the sweep's next commit under the sweep's message, which reads "out-of-band
    edit" — mis-attributing an agent write in the exact record this exists to
    make honest.
    """
    root = str(vault_root)
    rel_paths = [str(p) for p in paths if str(p)]
    if not rel_paths:
        return False
    if not is_git_vault(root):
        # The documented no-op. A non-git vault is a fully supported
        # deployment — it is the default — so this is not worth a log line on
        # every write.
        return False
    git = git_executable()
    if git is None:
        logger.warning(
            "GIT_VAULT_ENABLED is set but no git binary is on PATH; the vault "
            "write stands and nothing was committed.",
            extra={"reason": "git_missing"},
        )
        return False

    timeout = float(settings.git_commit_timeout_seconds)
    env = _git_env(author_name, author_email)
    _clear_stale_index_lock(root)
    specs = [_pathspec(p) for p in rel_paths]

    added = _run(git, root, ["add", "-A", "--", *specs], env, timeout)
    if added is None or added.returncode != 0:
        if added is not None:
            complaint = f"{added.stdout}\n{added.stderr}"
            if any(marker in complaint for marker in _ADD_NON_FAILURES):
                logger.debug(
                    "Nothing to stage for %s in the vault: %s",
                    rel_paths,
                    _first_line(added.stderr),
                )
            else:
                logger.warning(
                    "git add failed in the vault (exit %d): %s. The write "
                    "stands; the reconcile sweep will pick it up.",
                    added.returncode,
                    _first_line(added.stderr),
                    extra={"reason": "add_failed"},
                )
        # Unstage regardless of which branch above ran. A multi-path `add` can
        # stage some of its pathspecs before rejecting another, so "the add
        # failed" does not imply "nothing is staged" — and a path left staged
        # is swept into the sweep's next commit under a message claiming it was
        # edited outside the MCP server (rule 3).
        _unstage(git, root, specs, env, timeout)
        return False

    committed = _run(
        git,
        root,
        [
            "commit",
            "--only",
            "--quiet",
            # Hooks are skipped. `.git/hooks` is not shared by a clone, so a
            # hook here is one an operator installed on the *server* — and a
            # hook that prompts, lints or reformats would turn a tool call into
            # an arbitrary-length stall or an unexpected mutation of the note
            # that was just written.
            "--no-verify",
            # Signing is skipped for the same class of reason: `commit.gpgsign`
            # inherited from a config would make every agent write wait on a
            # gpg-agent that has no terminal to ask on.
            "--no-gpg-sign",
            "-m",
            message,
            "--",
            *specs,
        ],
        env,
        timeout,
    )
    if committed is not None and committed.returncode == 0:
        return True

    if committed is not None:
        blob = f"{committed.stdout}\n{committed.stderr}".lower()
        if any(marker in blob for marker in _NOTHING_TO_COMMIT):
            # A republication of identical bytes. Not a failure and not worth a
            # warning: the tool did what it was asked, and the tree already
            # says so.
            logger.debug("Nothing to commit for %s in the vault.", rel_paths)
            _unstage(git, root, specs, env, timeout)
            return False
        logger.warning(
            "git commit failed in the vault (exit %d): %s. The write stands; "
            "the reconcile sweep will pick it up.",
            committed.returncode,
            _first_line(committed.stderr) or _first_line(committed.stdout),
            extra={"reason": "commit_failed"},
        )
    _unstage(git, root, specs, env, timeout)
    return False


def _unstage(git: str, root: str, specs: list[str], env: dict, timeout: float) -> None:
    """Undo the staging of exactly `specs`. Best-effort, never raises.

    `git reset -q -- <paths>` resets those index entries to HEAD and touches
    nothing else — not other staged paths, and not the working tree, which
    still holds the bytes the write published and must keep holding them.
    """
    reset = _run(git, root, ["reset", "-q", "--", *specs], env, timeout)
    if reset is not None and reset.returncode != 0:
        logger.warning(
            "Could not unstage after a failed vault commit (exit %d): %s. The "
            "next reconcile sweep will commit these paths under its own "
            "message.",
            reset.returncode,
            _first_line(reset.stderr),
            extra={"reason": "unstage_failed"},
        )


def _first_line(text: str | None) -> str:
    """git's first line of complaint, bounded. Never a whole stderr dump."""
    if not text:
        return ""
    return _sanitise(text.strip().splitlines()[0] if text.strip() else "", 200)


# ── The async entry point ───────────────────────────────────────────────────

#: One git process at a time (rule 4). Module-global and process-local, with a
#: test-only reset beside it, exactly as `src/services/vault_overlap.py` does
#: it: an `asyncio.Lock` binds to the first event loop that awaits it and
#: refuses every other one, and the suite gives each test its own loop.
#: Production has one loop per process, so nothing there needs the reset.
_commit_lock = asyncio.Lock()


def reset_state_for_tests() -> None:
    """Replace the commit lock. **Tests only** — see `_commit_lock`."""
    global _commit_lock
    _commit_lock = asyncio.Lock()


async def commit_recorded(tool: str, principal: str | None) -> bool:
    """Commit whatever this tool call recorded. Returns whether it committed.

    The one thing `_tracked` calls. It is the flush half of `record_write`: the
    tool bodies say *what* was published, the decorator says *who* published it
    and *which tool* it was, and neither has to learn the other's job.

    Three properties hold here rather than at the call site:

    * **Off the event loop.** `subprocess.run` blocks, and a git invocation on
      the loop would stall every other request in this single-worker process
      for its whole duration. `asyncio.to_thread`, the same idiom the indexer
      uses for its blocking scans.
    * **Serialised.** The lock is taken around the whole add/commit pair, not
      around each half: two interleaved calls would otherwise stage each
      other's paths and commit them under one another's messages.
    * **Silent about its own failures.** The guard covers the WHOLE body, not
      merely the `to_thread` call, and that is deliberate: it makes "this
      function does not raise" a property of one function rather than a
      convention its caller has to re-implement. `_tracked` awaits it after the
      tool body has completed and its result is already decided, so a second
      `try` at the call site would be belt-and-braces over a guarantee made
      here — and a bare `logger.warning` in `src/mcp_server/tools.py`, which
      D18 forbids for a record a caller can drive.
    """
    try:
        recorded = pending_paths()
        if not recorded:
            return False
        roots = {root for root, _ in recorded}
        if len(roots) > 1:  # pragma: no cover - one call, one vault root
            # Defensive. A single tool call resolves one vault root; if that
            # ever stops being true, committing a mixture under one message
            # would be a false record, so refuse rather than guess.
            logger.warning(
                "A single %s call published under %d vault roots; nothing was "
                "committed and the reconcile sweep will pick the writes up.",
                tool,
                len(roots),
                extra={"reason": "mixed_roots", "tool": tool},
            )
            return False
        root = next(iter(roots))
        # Deduplicate, order-preservingly: `move_note` records the destination
        # once for the rename and again for the moved note's own link rewrite,
        # and a repeated pathspec is a repeated stat for no benefit.
        seen: set[str] = set()
        rel_paths = []
        for _, rel in recorded:
            if rel not in seen:
                seen.add(rel)
                rel_paths.append(rel)

        message = build_message(tool, rel_paths, principal)
        async with _commit_lock:
            return await asyncio.to_thread(
                commit_paths,
                rel_paths,
                message,
                settings.git_agent_name,
                settings.git_agent_email,
                root,
            )
    except Exception as exc:  # noqa: BLE001 - rule 1: never fail a write
        logger.warning(
            "Vault commit for %s failed unexpectedly (%s); the write stands "
            "and the reconcile sweep will pick it up.",
            tool,
            type(exc).__name__,
            extra={"reason": "commit_raised", "tool": tool, "error_type": type(exc).__name__},
        )
        return False
