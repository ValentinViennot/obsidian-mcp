"""Commit-on-write, end to end through the MCP tool bodies.

The vault here is a synthetic git repository built in `tmp_path` — `git init`,
one seed commit, invented note names — driven the way
`tests/test_vault_mutation_safety.py` drives one: `settings.vault_path`
monkeypatched at it, `current_permission` set, `_log_usage` stubbed out.

What each test is actually asserting is one of three things:

* the *right* commit appears (identity, subject, trailers, and exactly the
  paths the tool published — no more);
* **a git failure never costs the write** — the file is on disk and the tool's
  answer is unchanged whether git worked, was missing, or was refused;
* the feature is invisible when it is off, which is the default.
"""
import asyncio
import shutil
import subprocess

import pytest

import src.mcp_server.tools as tools
from src.auth.session import current_actor
from src.mcp_server.auth import current_permission
from src.services import git_vault

from tests.test_git_vault import git, head_body, log_subjects, make_repo, porcelain


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)


@pytest.fixture(autouse=True)
def fresh_commit_lock():
    """The commit lock binds to one event loop; the suite gives each test its own."""
    git_vault.reset_state_for_tests()
    yield
    git_vault.reset_state_for_tests()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A git-backed vault with commit-on-write on, and a named principal."""
    root = make_repo(tmp_path / "vault")
    monkeypatch.setattr(tools.settings, "vault_path", str(root))
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)
    monkeypatch.setattr(git_vault.settings, "git_agent_name", "obsidian-mcp agent")
    monkeypatch.setattr(git_vault.settings, "git_agent_email", "agent@test.invalid")
    monkeypatch.setattr(git_vault.settings, "git_commit_timeout_seconds", 15.0)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")
    actor = current_actor.set(("api_key", "laptop-key", "omcp_abc123"))
    yield root
    current_actor.reset(actor)
    current_permission.reset(permission)


@pytest.fixture
def plain_vault(tmp_path, monkeypatch):
    """The same, on a directory that is not a git repository at all."""
    root = tmp_path / "plain"
    root.mkdir()
    monkeypatch.setattr(tools.settings, "vault_path", str(root))
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", True)
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", True)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")
    yield root
    current_permission.reset(permission)


def committed_paths(repo, ref="HEAD"):
    out = git(repo, "show", "--name-status", "--no-renames", "--format=", ref).stdout
    return {
        parts[1]: parts[0]
        for parts in (line.split("\t") for line in out.strip().splitlines())
        if len(parts) == 2
    }


def head_identity(repo):
    return git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()


# ── One commit per write path ───────────────────────────────────────────────


async def test_create_note_commits(vault):
    result = await tools.create_note_impl("Notes/Alpha.md", "# Alpha\n")

    assert result.startswith("Created note: Notes/Alpha.md")
    assert log_subjects(vault)[0] == "mcp(create_note): create Notes/Alpha.md"
    assert committed_paths(vault) == {"Notes/Alpha.md": "A"}
    assert head_identity(vault) == "obsidian-mcp agent <agent@test.invalid>"
    assert "Tool: create_note" in head_body(vault)
    assert "Principal: laptop-key" in head_body(vault)
    assert porcelain(vault) == ""


async def test_edit_note_commits(vault):
    await tools.create_note_impl("Alpha.md", "one\n")
    await tools.edit_note_impl("Alpha.md", content="two\n")

    assert log_subjects(vault)[0] == "mcp(edit_note): update Alpha.md"
    assert committed_paths(vault) == {"Alpha.md": "M"}
    assert (vault / "Alpha.md").read_text(encoding="utf-8") == "two\n"


async def test_set_frontmatter_commits(vault):
    await tools.create_note_impl("Alpha.md", "body\n")
    await tools.set_frontmatter_impl("Alpha.md", updates={"status": "done"})

    assert log_subjects(vault)[0] == "mcp(set_frontmatter): update frontmatter of Alpha.md"
    assert committed_paths(vault) == {"Alpha.md": "M"}


async def test_write_file_commits(vault):
    await tools.write_file_impl("assets/data.json", '{"a":1}', encoding="text")

    assert log_subjects(vault)[0] == "mcp(write_file): write assets/data.json"
    assert committed_paths(vault) == {"assets/data.json": "A"}


async def test_move_note_commits_both_ends(vault):
    await tools.create_note_impl("Old.md", "body\n")
    result = await tools.move_note_impl("Old.md", "Archive/New.md")

    assert result.startswith("Moved Old.md → Archive/New.md")
    assert log_subjects(vault)[0] == "mcp(move_note): move Old.md → Archive/New.md"
    # Both ends, so `git log -- Old.md` finds the commit that removed it.
    assert committed_paths(vault) == {"Old.md": "D", "Archive/New.md": "A"}
    assert porcelain(vault) == ""


class _EmptyResult:
    def all(self):
        return []


class _FakeSession:
    """The backlink queries `move_note(rewrite_links=True)` issues, answered empty.

    Copied in spirit from `tests/test_vault_mutation_safety.py`: these tests are
    about what reaches git, and standing up Postgres to learn that a note has no
    backlinks would make them a different kind of test.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _EmptyResult()

    async def commit(self):
        return None


async def test_move_note_commits_its_own_link_rewrite_once(vault, monkeypatch):
    """The rename and the rewrites land in ONE commit, with no duplicate path.

    One commit and not two: they are one operation the agent asked for once, and
    splitting them would put a state in the history — "renamed, links not yet
    updated" — that never existed on disk. The moved note is recorded twice
    (once by the rename, once by its own rewrite) and must appear once.
    """
    monkeypatch.setattr(tools, "async_session", _FakeSession)
    await tools.create_note_impl("old/Target.md", "Self: [[old/Target]]\n")

    result = await tools.move_note_impl(
        "old/Target.md", "new/Target.md", rewrite_links=True
    )

    assert "Moved" in result
    assert (vault / "new" / "Target.md").read_text(encoding="utf-8") == (
        "Self: [[new/Target]]\n"
    )
    assert log_subjects(vault)[0] == (
        "mcp(move_note): move old/Target.md → new/Target.md"
    )
    assert committed_paths(vault) == {"old/Target.md": "D", "new/Target.md": "A"}
    # Two paths, not three: the destination was recorded twice and is deduped.
    assert head_body(vault).count("new/Target.md") == 1
    assert porcelain(vault) == ""


async def test_delete_note_soft_commits_the_deletion(vault):
    await tools.create_note_impl("Doomed.md", "bye\n")
    result = await tools.delete_note_impl("Doomed.md")

    assert result.startswith("Soft-deleted: Doomed.md")
    assert log_subjects(vault)[0] == "mcp(delete_note): delete Doomed.md"
    # Only the source path. The `.trash/` copy is deliberately not recorded —
    # `deploy/vault.gitignore` excludes `.trash/` (see
    # `tests/test_vault_gitignore_template.py`), so on a real vault it is not
    # even a candidate. This synthetic repo has no ignore file, which is why
    # the trash copy shows as untracked below rather than as nothing at all.
    assert committed_paths(vault) == {"Doomed.md": "D"}
    assert porcelain(vault).startswith("?? .trash/")


async def test_delete_note_permanent_commits_the_deletion(vault):
    await tools.create_note_impl("Doomed.md", "bye\n")
    await tools.delete_note_impl("Doomed.md", permanent=True)

    assert log_subjects(vault)[0] == "mcp(delete_note): delete Doomed.md"
    assert committed_paths(vault) == {"Doomed.md": "D"}
    assert porcelain(vault) == ""


async def test_delete_file_commits_the_deletion(vault):
    await tools.write_file_impl("assets/data.json", "{}", encoding="text")
    await tools.delete_file_impl("assets/data.json", permanent=True)

    assert log_subjects(vault)[0] == "mcp(delete_file): delete assets/data.json"
    assert committed_paths(vault) == {"assets/data.json": "D"}


# ── Nothing published, nothing committed ────────────────────────────────────


async def test_a_refused_write_commits_nothing(vault):
    await tools.create_note_impl("Alpha.md", "one\n")
    before = log_subjects(vault)

    # A second create at the same path publishes nothing.
    result = await tools.create_note_impl("Alpha.md", "two\n")

    assert "already exists" in result
    assert log_subjects(vault) == before


async def test_a_dry_run_commits_nothing(vault):
    await tools.create_note_impl("Alpha.md", "one\n")
    before = log_subjects(vault)

    await tools.edit_note_impl("Alpha.md", content="two\n", dry_run=True)

    assert log_subjects(vault) == before
    assert (vault / "Alpha.md").read_text(encoding="utf-8") == "one\n"


async def test_a_read_tool_commits_nothing(vault):
    await tools.create_note_impl("Alpha.md", "one\n")
    before = log_subjects(vault)

    await tools.read_note_impl("Alpha.md")

    assert log_subjects(vault) == before


async def test_an_unchanged_rewrite_commits_nothing(vault):
    """`set_frontmatter` that changes nothing returns early and publishes nothing."""
    await tools.create_note_impl("Alpha.md", "---\nstatus: done\n---\nbody\n")
    before = log_subjects(vault)

    result = await tools.set_frontmatter_impl("Alpha.md", updates={"status": "done"})

    assert "No changes" in result
    assert log_subjects(vault) == before


# ── Off by default, and invisible when off ──────────────────────────────────


async def test_nothing_is_committed_when_the_feature_is_off(vault, monkeypatch):
    monkeypatch.setattr(git_vault.settings, "git_vault_enabled", False)

    result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note")
    assert log_subjects(vault) == ["seed"]
    assert porcelain(vault) == "?? Alpha.md"


async def test_commit_on_write_can_be_turned_off_on_its_own(vault, monkeypatch):
    monkeypatch.setattr(git_vault.settings, "git_commit_on_write", False)

    await tools.create_note_impl("Alpha.md", "one\n")

    assert log_subjects(vault) == ["seed"]


# ── A git failure never costs the write ─────────────────────────────────────


async def test_a_non_git_vault_writes_normally(plain_vault):
    result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note: Alpha.md")
    assert (plain_vault / "Alpha.md").read_text(encoding="utf-8") == "one\n"
    assert not (plain_vault / ".git").exists()


async def test_a_missing_git_binary_does_not_fail_the_write(vault, monkeypatch, caplog):
    monkeypatch.setattr(git_vault.shutil, "which", lambda _name: None)

    with caplog.at_level("WARNING"):
        result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note: Alpha.md")
    assert (vault / "Alpha.md").read_text(encoding="utf-8") == "one\n"
    assert "no git binary" in caplog.text
    assert log_subjects(vault) == ["seed"]


async def test_a_failing_git_does_not_fail_the_write(vault, monkeypatch, caplog):
    """The whole point of rule 1, exercised through the tool.

    The bytes are published by `_atomic_write_at` before anything here runs, so
    a commit failure has nothing to roll back — and an `edit_note` that answered
    "failed" for a write that stood would send the agent into a retry loop over
    a note it had already changed.
    """
    def refuse(*_a, **_kw):
        return subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: no\n")

    monkeypatch.setattr(git_vault.subprocess, "run", refuse)

    with caplog.at_level("WARNING"):
        result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note: Alpha.md")
    assert (vault / "Alpha.md").read_text(encoding="utf-8") == "one\n"
    assert "git add failed" in caplog.text


async def test_a_git_that_raises_does_not_fail_the_write(vault, monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise RuntimeError("the executor is gone")

    monkeypatch.setattr(git_vault, "commit_paths", boom)

    with caplog.at_level("WARNING"):
        result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note: Alpha.md")
    assert (vault / "Alpha.md").read_text(encoding="utf-8") == "one\n"
    assert "failed unexpectedly" in caplog.text


async def test_a_commit_timeout_does_not_fail_the_write(vault, monkeypatch, caplog):
    def stall(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr(git_vault.subprocess, "run", stall)

    with caplog.at_level("WARNING"):
        result = await tools.create_note_impl("Alpha.md", "one\n")

    assert result.startswith("Created note: Alpha.md")
    assert "timed out" in caplog.text


# ── Concurrency ─────────────────────────────────────────────────────────────


async def test_concurrent_writes_do_not_corrupt_the_index(vault):
    """Twelve tool calls at once, one commit each, and a clean tree at the end.

    Without the lock these interleave inside git's index: `add` from one call
    and `commit --only` from another race the same `.git/index`, and the
    observable damage is a commit carrying somebody else's path (so the
    attribution is wrong) or an `index.lock` collision that loses commits
    entirely.
    """
    paths = [f"Concurrent/n{i}.md" for i in range(12)]

    results = await asyncio.gather(
        *(tools.create_note_impl(path, f"body {i}\n") for i, path in enumerate(paths))
    )

    assert all(r.startswith("Created note:") for r in results)
    # Every file is on disk with its own content...
    for i, path in enumerate(paths):
        assert (vault / path).read_text(encoding="utf-8") == f"body {i}\n"
    # ...one commit each, each naming exactly its own path...
    subjects = log_subjects(vault)
    assert len(subjects) == len(paths) + 1  # + seed
    for sha in git(vault, "rev-list", "HEAD", f"-{len(paths)}").stdout.split():
        assert len(committed_paths(vault, sha)) == 1
    # ...and nothing left staged, half-staged, or untracked.
    assert porcelain(vault) == ""
    assert git(vault, "fsck", "--no-progress").returncode == 0


async def test_concurrent_writes_survive_a_stale_index_lock(vault, monkeypatch):
    """A crashed git's leftover lock is cleared once, not once per waiter."""
    import os
    import time

    lock = vault / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - git_vault.STALE_INDEX_LOCK_SECONDS - 60
    os.utime(lock, (old, old))

    await asyncio.gather(
        *(tools.create_note_impl(f"n{i}.md", f"body {i}\n") for i in range(4))
    )

    assert not lock.exists()
    assert len(log_subjects(vault)) == 5
    assert porcelain(vault) == ""


# ── Attribution ─────────────────────────────────────────────────────────────


async def test_an_oauth_client_name_is_the_principal(vault):
    token = current_actor.set(("oauth", "Claude Desktop", "client-abc"))
    try:
        await tools.create_note_impl("Alpha.md", "one\n")
    finally:
        current_actor.reset(token)

    assert "Principal: Claude Desktop" in head_body(vault)


async def test_a_caller_with_no_credential_is_recorded_as_unknown(vault):
    token = current_actor.set(None)
    try:
        await tools.create_note_impl("Alpha.md", "one\n")
    finally:
        current_actor.reset(token)

    assert "Principal: unknown" in head_body(vault)


async def test_a_hostile_client_name_cannot_forge_the_trailers(vault):
    """An OAuth client chooses its own name at dynamic registration."""
    token = current_actor.set(("oauth", "x\nPrincipal: the-owner", "client-abc"))
    try:
        await tools.create_note_impl("Alpha.md", "one\n")
    finally:
        current_actor.reset(token)

    body = head_body(vault)
    assert len([ln for ln in body.splitlines() if ln.startswith("Principal:")]) == 1
    # `git interpret-trailers` sees one Principal, and its value is the whole
    # folded string — not "the-owner".
    parsed = git(vault, "log", "-1", "--format=%(trailers:key=Principal,valueonly)").stdout
    assert parsed.strip() == "x Principal: the-owner"
