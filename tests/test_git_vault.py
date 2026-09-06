"""`src/services/git_vault.py`: the commit primitive, in isolation.

Every repository here is synthetic and built in `tmp_path` — `git init`, one
seed commit, invented note names — the same way the rest of the suite builds
ephemeral vaults. Nothing reads a real vault.

The tool-level wiring (which write paths record what, and what the resulting
commit says) lives in `tests/test_git_vault_commit_on_write.py`; the shell
sweep in `tests/test_vault_reconcile_script.py`.
"""
import os
import shutil
import subprocess
import time

import pytest

from src.services import git_vault


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)


# ── Synthetic repositories ──────────────────────────────────────────────────


def git(repo, *args, check=True):
    """One git call against `repo`, with a fixed identity and no user config.

    `-c` rather than the environment for the seed identity, so a test can never
    be mistaken for the production path (which sets `GIT_AUTHOR_*` instead) and
    so nothing here depends on the developer's own `~/.gitconfig`.
    """
    return subprocess.run(
        [
            "git",
            "-c", "user.name=Seed",
            "-c", "user.email=seed@example.invalid",
            "-c", f"safe.directory={repo}",
            "-C", str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=check,
    )


def make_repo(path):
    """An initialised repository with one commit, so HEAD is born."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed")
    return path


def log_subjects(repo):
    out = git(repo, "log", "--format=%s")
    return out.stdout.strip().splitlines()


def head_body(repo):
    return git(repo, "log", "-1", "--format=%B").stdout


def head_author(repo):
    out = git(repo, "log", "-1", "--format=%an|%ae|%cn|%ce").stdout.strip()
    return out.split("|")


def porcelain(repo):
    return git(repo, "status", "--porcelain").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "vault")


@pytest.fixture(autouse=True)
def default_timeout(monkeypatch):
    monkeypatch.setattr(git_vault.settings, "git_commit_timeout_seconds", 15.0)


def commit(repo, paths, message="mcp(edit_note): update x.md\n\nTool: edit_note\nPrincipal: k\n"):
    return git_vault.commit_paths(
        paths, message, "Agent", "agent@example.invalid", str(repo)
    )


# ── The happy path ──────────────────────────────────────────────────────────


def test_a_commit_is_made_with_the_supplied_identity(repo):
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    assert commit(repo, ["note.md"]) is True

    assert log_subjects(repo)[0] == "mcp(edit_note): update x.md"
    author, email, committer, committer_email = head_author(repo)
    # Author *and* committer, both. Only setting the author leaves the
    # committer to `user.name`, which on a machine with no git config is
    # git's hostname-derived guess and on the owner's desktop would be the
    # owner — the exact confusion this feature exists to remove.
    assert (author, email) == ("Agent", "agent@example.invalid")
    assert (committer, committer_email) == ("Agent", "agent@example.invalid")
    assert porcelain(repo) == ""


def test_the_repository_config_is_not_touched(repo):
    """The identity travels in the environment and leaves no trace behind.

    The desktop clones this repository; a `user.name` written into `.git/config`
    here would not travel, but a *habit* of writing git config from the server
    is one `--global` away from relabelling every commit the human makes.
    """
    before = (repo / ".git" / "config").read_text(encoding="utf-8")
    (repo / "note.md").write_text("hello\n", encoding="utf-8")
    assert commit(repo, ["note.md"]) is True
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == before
    # Asked without the helper's own `-c` overrides, so this reads what is
    # actually configured for the repository and nothing else.
    asked = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-regexp", "^user\\."],
        capture_output=True, text=True, check=False,
    )
    assert asked.stdout == ""


def test_only_the_named_paths_are_committed(repo):
    """An operator's own staged work is not swept into an agent's commit."""
    (repo / "ours.md").write_text("ours\n", encoding="utf-8")
    (repo / "theirs.md").write_text("theirs\n", encoding="utf-8")
    git(repo, "add", "theirs.md")

    assert commit(repo, ["ours.md"]) is True

    committed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert committed == ["ours.md"]
    # Still staged, untouched, exactly as the operator left it.
    assert "A  theirs.md" in porcelain(repo)


def test_a_deletion_is_committed(repo):
    (repo / "gone.md").write_text("bye\n", encoding="utf-8")
    assert commit(repo, ["gone.md"]) is True
    os.unlink(repo / "gone.md")

    assert commit(repo, ["gone.md"], "mcp(delete_note): delete gone.md\n") is True

    status = git(repo, "show", "--name-status", "--format=", "HEAD").stdout
    assert status.split() == ["D", "gone.md"]


def test_a_move_commits_both_ends(repo):
    (repo / "old.md").write_text("body\n", encoding="utf-8")
    assert commit(repo, ["old.md"]) is True
    os.rename(repo / "old.md", repo / "new.md")

    assert commit(repo, ["old.md", "new.md"], "mcp(move_note): move old.md → new.md\n") is True

    # `--no-renames`, so the commit is described as what it has to be for
    # `git log -- <old path>` to find it: a deletion at one path and an
    # addition at the other. (With rename detection on, git collapses the pair
    # into one `R` entry — which is a *rendering* of the same commit, not
    # evidence that both ends were staged.)
    status = git(repo, "show", "--name-status", "--no-renames", "--format=", "HEAD").stdout
    assert set(status.split()) == {"A", "new.md", "D", "old.md"}
    assert porcelain(repo) == ""


def test_a_first_commit_works_on_an_unborn_head(tmp_path):
    """A freshly `git init`-ed vault has no HEAD; a partial commit still works.

    Worth pinning: `git commit --only -- <paths>` is a *partial* commit, and
    partial commits are refused in several states (mid-merge, mid-rebase). An
    unborn HEAD is not one of them, but nothing in git's documentation says so
    and an operator's very first agent write lands exactly here.
    """
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "first.md").write_text("first\n", encoding="utf-8")

    assert commit(repo, ["first.md"]) is True
    assert log_subjects(repo) == ["mcp(edit_note): update x.md"]


# ── The no-ops ──────────────────────────────────────────────────────────────


def test_a_non_git_vault_is_a_clean_no_op(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "note.md").write_text("hello\n", encoding="utf-8")

    assert commit(plain, ["note.md"]) is False
    assert (plain / "note.md").read_text(encoding="utf-8") == "hello\n"
    assert not (plain / ".git").exists()


def test_a_vault_merely_inside_a_repository_is_not_committed(repo):
    """The enclosing repository is left alone.

    `git -C <subdir>` walks up and finds the enclosing `.git` quite happily, so
    without the explicit root test a vault configured as a subdirectory of some
    other repository would start committing that repository.
    """
    nested = repo / "sub"
    nested.mkdir()
    (nested / "note.md").write_text("hello\n", encoding="utf-8")

    assert commit(nested, ["note.md"]) is False
    assert log_subjects(repo) == ["seed"]


def test_no_git_binary_is_a_clean_no_op(repo, monkeypatch, caplog):
    monkeypatch.setattr(git_vault.shutil, "which", lambda _name: None)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is False

    assert "no git binary" in caplog.text
    # The write stands, and the tree is exactly as the write left it.
    assert (repo / "note.md").read_text(encoding="utf-8") == "hello\n"


def test_nothing_to_commit_is_not_a_failure(repo, caplog):
    (repo / "note.md").write_text("hello\n", encoding="utf-8")
    assert commit(repo, ["note.md"]) is True

    with caplog.at_level("WARNING"):
        # A republication of byte-identical content.
        assert commit(repo, ["note.md"]) is False

    assert caplog.text == ""
    assert len(log_subjects(repo)) == 2  # seed + the one real commit
    assert porcelain(repo) == ""


def test_writing_to_an_ignored_path_is_a_quiet_no_op(repo, caplog):
    """`deploy/vault.gitignore` excludes paths an agent may legitimately write.

    `git add` on an *explicitly named* ignored path is an error, unlike the
    silent skip a bare `add -A` performs — so without this branch every
    `write_file(".obsidian/workspace.json", …)` would log a warning saying only
    that the ignore file worked.
    """
    (repo / ".gitignore").write_text(".trash/\n.obsidian/workspace.json\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore file")
    (repo / ".obsidian").mkdir()
    (repo / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, [".obsidian/workspace.json"]) is False

    assert caplog.text == ""
    assert porcelain(repo) == ""


def test_a_path_that_is_neither_on_disk_nor_tracked_is_a_quiet_no_op(repo, caplog):
    """A delete of a file whose creation never got committed."""
    with caplog.at_level("WARNING"):
        assert commit(repo, ["never-existed.md"]) is False

    assert caplog.text == ""
    assert porcelain(repo) == ""


def test_an_empty_path_list_does_nothing(repo):
    assert commit(repo, []) is False
    assert commit(repo, [""]) is False
    assert log_subjects(repo) == ["seed"]


# ── Failure never costs the write, and never leaves the index dirty ─────────


def _fail_the_commit(monkeypatch):
    """Let `add` through, make `commit` fail. Leaves `reset` working."""
    real = git_vault._run

    def fake(git_bin, root, args, env, timeout):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="", stderr="fatal: nope\n"
            )
        return real(git_bin, root, args, env, timeout)

    monkeypatch.setattr(git_vault, "_run", fake)


def test_a_failed_commit_leaves_nothing_staged(repo, monkeypatch, caplog):
    """Rule 3. A path left staged is swept into the next *sweep* commit, under
    the sweep's "changed outside the MCP server" message — mis-attributing an
    agent write in the one record that exists to be honest about who wrote
    what."""
    _fail_the_commit(monkeypatch)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is False

    assert "git commit failed" in caplog.text
    # Untracked, not staged: the `add` is undone.
    assert porcelain(repo) == "?? note.md"
    # And the bytes are still there. Nothing about a git failure touches them.
    assert (repo / "note.md").read_text(encoding="utf-8") == "hello\n"


def test_a_failed_commit_does_not_unstage_the_operators_own_work(repo, monkeypatch):
    """The unstage is scoped to the paths this call staged, and no others."""
    _fail_the_commit(monkeypatch)
    (repo / "theirs.md").write_text("theirs\n", encoding="utf-8")
    git(repo, "add", "theirs.md")
    (repo / "ours.md").write_text("ours\n", encoding="utf-8")

    assert commit(repo, ["ours.md"]) is False

    status = porcelain(repo)
    assert "A  theirs.md" in status
    assert "?? ours.md" in status


def test_a_broken_repository_never_reaches_the_caller(repo, caplog):
    """The realistic production failure: git itself refuses, for real.

    A corrupt `.git/config` makes *every* git invocation fail at startup, which
    is the shape of the whole class — a repository that has been damaged, a
    filesystem that has gone read-only, a half-finished `git gc`. Driven with
    the real binary rather than a monkeypatched `_run`, so the swallow is
    proved against git's actual exit codes and messages.

    `commit_paths` must answer False and log — never raise — because by the
    time it runs the note is already durably on disk and its caller has already
    decided what to tell the agent.
    """
    (repo / "note.md").write_text("hello\n", encoding="utf-8")
    (repo / ".git" / "config").write_text("[core\nthis is not ini\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is False

    assert "git add failed" in caplog.text
    assert (repo / "note.md").read_text(encoding="utf-8") == "hello\n"


def test_a_timeout_is_swallowed(repo, monkeypatch, caplog):
    def slow(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr(git_vault.subprocess, "run", slow)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is False

    assert "timed out" in caplog.text


def test_a_spawn_failure_is_swallowed(repo, monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise OSError(12, "Cannot allocate memory")

    monkeypatch.setattr(git_vault.subprocess, "run", boom)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is False

    assert "Could not run git" in caplog.text


# ── The stale index lock ────────────────────────────────────────────────────


def test_a_fresh_index_lock_is_left_alone(repo):
    """Somebody else's git is running. Removing its lock corrupts the index."""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    assert commit(repo, ["note.md"]) is False
    assert lock.exists()


def test_a_stale_index_lock_is_cleared_and_the_commit_proceeds(repo, caplog):
    """A git killed mid-commit would otherwise end commit-on-write for ever."""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - git_vault.STALE_INDEX_LOCK_SECONDS - 60
    os.utime(lock, (old, old))
    (repo / "note.md").write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert commit(repo, ["note.md"]) is True

    assert "stale .git/index.lock" in caplog.text
    assert not lock.exists()


def test_a_gitlink_worktree_is_never_guessed_at(tmp_path):
    """`.git` as a file names a git directory elsewhere; do not go looking."""
    fake = tmp_path / "linked"
    fake.mkdir()
    (fake / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    assert git_vault.is_git_vault(fake) is True
    # No exception, and nothing removed — there is no `.git/index.lock` to
    # reason about when `.git` is not a directory.
    git_vault._clear_stale_index_lock(str(fake))


# ── Pathspec safety ─────────────────────────────────────────────────────────


def test_a_filename_that_looks_like_pathspec_magic_is_taken_literally(repo):
    """`--` does not disable pathspec magic; `:(literal)` does.

    Without it, a note named `:(exclude)secret.md` would turn "commit this one
    file" into "commit everything except it".
    """
    hostile = ":(exclude)secret.md"
    (repo / hostile).write_text("payload\n", encoding="utf-8")
    (repo / "bystander.md").write_text("unrelated\n", encoding="utf-8")

    assert commit(repo, [hostile]) is True

    names = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split("\n")
    assert hostile in [n.strip('"') for n in names if n]
    assert "?? bystander.md" in porcelain(repo)


def test_a_filename_with_a_glob_is_taken_literally(repo):
    (repo / "star*.md").write_text("literal\n", encoding="utf-8")
    (repo / "starlight.md").write_text("decoy\n", encoding="utf-8")

    assert commit(repo, ["star*.md"]) is True

    assert "?? starlight.md" in porcelain(repo)


# ── The message ─────────────────────────────────────────────────────────────


def test_the_message_shape_is_subject_blank_trailers():
    message = git_vault.build_message("edit_note", ["Projects/Roadmap.md"], "laptop-key")
    assert message.splitlines() == [
        "mcp(edit_note): update Projects/Roadmap.md",
        "",
        "Tool: edit_note",
        "Principal: laptop-key",
    ]


def test_a_move_names_both_ends_in_the_subject():
    message = git_vault.build_message("move_note", ["a/Old.md", "b/New.md"], "k")
    assert message.splitlines()[0] == "mcp(move_note): move a/Old.md → b/New.md"


def test_many_paths_are_summarised_in_the_subject_and_listed_in_the_body():
    paths = [f"n{i}.md" for i in range(4)]
    lines = git_vault.build_message("move_note", paths, "k").splitlines()
    assert lines[0] == "mcp(move_note): move 4 paths"
    assert lines[2:6] == ["  n0.md", "  n1.md", "  n2.md", "  n3.md"]


def test_a_very_long_body_path_list_is_elided():
    many = git_vault.MAX_BODY_PATHS + 5
    paths = [f"n{i}.md" for i in range(many)]
    body = git_vault.build_message("move_note", paths, "k")
    assert f"… and {many - git_vault.MAX_BODY_PATHS} more" in body


def test_a_missing_principal_reads_as_unknown():
    assert "Principal: unknown" in git_vault.build_message("create_note", ["a.md"], None)


@pytest.mark.parametrize(
    "hostile",
    [
        "evil\nPrincipal: someone-else",
        "evil\r\nTool: create_note",
        "evil Principal: nobody",
        "evil\x00Principal: nobody",
    ],
)
def test_a_principal_cannot_forge_a_trailer(hostile):
    """An OAuth client names *itself* at dynamic registration.

    A client called "x\\nPrincipal: someone-else" would otherwise write its own
    attribution into every commit made on its behalf — in the exact field an
    operator reads to find out who did something.
    """
    message = git_vault.build_message("create_note", ["a.md"], hostile)
    lines = message.splitlines()
    # Exactly one line *begins* a `Principal` trailer — which is the only form
    # git's trailer parser recognises. The forged text survives as inert
    # characters inside that one line's value.
    assert [ln for ln in lines if ln.startswith("Principal:")] == [
        f"Principal: {' '.join(hostile.replace(chr(0), ' ').split())}"
    ]
    assert lines[-1].startswith("Principal:")


def test_a_path_with_a_newline_cannot_break_the_subject():
    message = git_vault.build_message("create_note", ["a\nTool: rm -rf.md"], "k")
    lines = message.splitlines()
    # The path folded onto the subject line; the trailer block is still exactly
    # the two lines this module wrote.
    assert [ln for ln in lines if ln.startswith(("Tool:", "Principal:"))] == [
        "Tool: create_note",
        "Principal: k",
    ]


def test_the_subject_is_bounded():
    long_path = "d/" * 400 + "note.md"
    subject = git_vault.build_message("edit_note", [long_path], "k").splitlines()[0]
    assert len(subject) <= git_vault.MAX_SUBJECT_CHARS
    assert subject.endswith("…")


# ── The accumulator ─────────────────────────────────────────────────────────


def test_recording_outside_a_tool_call_is_dropped(monkeypatch):
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
    git_vault.record_write("/vault", "a.md")
    assert git_vault.pending_paths() == []


def test_recording_is_off_unless_both_switches_are_on(monkeypatch):
    token = git_vault.begin()
    try:
        monkeypatch.setattr(git_vault.settings, "git_vault_enabled", False)
        monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
        git_vault.record_write("/vault", "a.md")
        assert git_vault.pending_paths() == []

        monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
        monkeypatch.setattr(git_vault.settings, "git_commit_on_write", False)
        git_vault.record_write("/vault", "b.md")
        assert git_vault.pending_paths() == []

        monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
        git_vault.record_write("/vault", "c.md")
        assert git_vault.pending_paths() == [("/vault", "c.md")]
    finally:
        git_vault.clear(token)


async def test_commit_recorded_dedupes_and_commits_once(repo, monkeypatch):
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
    monkeypatch.setattr(git_vault.settings, "git_agent_name", "Agent")
    monkeypatch.setattr(git_vault.settings, "git_agent_email", "agent@example.invalid")
    git_vault.reset_state_for_tests()

    (repo / "a.md").write_text("a\n", encoding="utf-8")
    (repo / "b.md").write_text("b\n", encoding="utf-8")
    token = git_vault.begin()
    try:
        git_vault.record_write(repo, "a.md")
        git_vault.record_write(repo, "b.md")
        git_vault.record_write(repo, "a.md")  # move_note records a path twice
        assert await git_vault.commit_recorded("move_note", "laptop-key") is True
    finally:
        git_vault.clear(token)

    assert len(log_subjects(repo)) == 2
    names = git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(names) == ["a.md", "b.md"]
    assert "Principal: laptop-key" in head_body(repo)


async def test_commit_recorded_refuses_a_mixture_of_vault_roots(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
    git_vault.reset_state_for_tests()
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")

    token = git_vault.begin()
    try:
        git_vault.record_write(one, "a.md")
        git_vault.record_write(two, "b.md")
        with caplog.at_level("WARNING"):
            assert await git_vault.commit_recorded("edit_note", "k") is False
    finally:
        git_vault.clear(token)

    assert "vault roots" in caplog.text
    assert log_subjects(one) == ["seed"]
    assert log_subjects(two) == ["seed"]


async def test_commit_recorded_swallows_an_unexpected_failure(repo, monkeypatch, caplog):
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
    git_vault.reset_state_for_tests()

    def boom(*_a, **_kw):
        raise RuntimeError("the thread pool is gone")

    monkeypatch.setattr(git_vault, "commit_paths", boom)
    token = git_vault.begin()
    try:
        git_vault.record_write(repo, "a.md")
        with caplog.at_level("WARNING"):
            assert await git_vault.commit_recorded("edit_note", "k") is False
    finally:
        git_vault.clear(token)

    assert "failed unexpectedly" in caplog.text
