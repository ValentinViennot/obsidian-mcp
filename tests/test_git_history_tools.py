"""The three git-history tools: `note_history`, `note_blame`, `find_when_written`.

The vault is becoming a git repository with reconstructed history, and these
tools answer "when, and by whom, was this written?" from it. Everything here
runs against **synthetic repositories built in a tmp dir**, never a real
vault, with `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` set on every commit so
that every date assertion is exact rather than "recent".

The fixture history is deliberately constructed to make each claim falsifiable
on its own:

* `Projects/Kickoff.md` is **born** as `Inbox/Kickoff.md` in the import commit
  and renamed one commit later — so a `--follow` that stopped at the rename
  would report the wrong creation date, and the birth-commit assertions would
  fail rather than merely look different.
* Two authors write different lines of it, so an attribution that collapsed to
  "whoever touched it last" is visible.
* A **bulk reformat** commit rewrites a bullet marker (a real content change,
  not whitespace, so `-w` cannot rescue it) and is named in
  `.git-blame-ignore-revs`. One test asserts the attribution *with* the file
  and one *without*, because "ignore-revs is applied" is only meaningful
  beside the answer it changes.
* One commit edits the note **without** changing the number of occurrences of
  the decision sentence, which is what separates the pickaxe from a grep over
  history.

Setup convention follows `tests/test_file_access_tools.py`: minimal env
defaults and a chdir away from any `.env` BEFORE importing the tools module.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.services import git_history  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="the history tools need a git executable"
)

ALICE = ("Alice Import", "alice@example.invalid")
BOB = ("Bob Decider", "bob@example.invalid")
CAROL = ("Carol Formatter", "carol@example.invalid")
DAVE = ("Dave Reviser", "dave@example.invalid")

IMPORT_DATE = "2019-03-04T09:12:00+01:00"
RENAME_DATE = "2020-01-02T03:04:05+00:00"
DECISION_DATE = "2021-06-01T10:00:00+02:00"
REFORMAT_DATE = "2022-02-02T12:00:00+00:00"
IGNORE_DATE = "2022-03-03T08:00:00+01:00"
# Deliberately not UTC: git's `%aI` renders a UTC commit as `…Z` rather than
# `…+00:00`, and a fixture that only ever used UTC could not tell a preserved
# author offset from the server's own timezone leaking in.
REVISION_DATE = "2023-05-05T05:05:05+03:00"

DECISION = "we chose pgvector over qdrant"

KICKOFF_AT_IMPORT = "# Kickoff\n\n## Notes\n* first bullet\n* second bullet\n"
KICKOFF_AT_DECISION = KICKOFF_AT_IMPORT + f"\n## Decision\n{DECISION}\n"
KICKOFF_AT_REFORMAT = KICKOFF_AT_DECISION.replace("* ", "- ")
KICKOFF_FINAL = KICKOFF_AT_REFORMAT.replace(
    "- first bullet", "- first bullet, revised"
)


# ── building a repository ────────────────────────────────────────────────────


def _git(root: Path, *args: str, env: dict | None = None) -> str:
    """Run git in `root` under a scrubbed environment.

    Hermetic on purpose: the developer's own `user.name`, `init.defaultBranch`
    and any `GIT_*` in their shell would otherwise decide what these
    assertions see. `commit.gpgsign=false` matters on a machine that signs by
    default — a signature prompt would hang the suite rather than fail it.
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


def _commit(root: Path, message: str, when: str, who: tuple[str, str]) -> str:
    """Commit everything staged at an exact author *and* committer date.

    Both are pinned. A reconstructed history has an authored date that is the
    note's real age and a committed date that is when the import ran, and the
    tools report both — so a fixture that pinned only one could not tell a
    swapped pair from a correct one.
    """
    name, email = who
    _git(
        root,
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "commit", "-q", "-m", message,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    return _git(root, "rev-parse", "HEAD").strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class _Vault(Path):
    """A vault root that also carries the fixture's commit shas.

    Python 3.12 supports subclassing `Path`, and every test here wants both
    halves — the directory to write into and the shas to assert against — so a
    tuple or a namespace would just make every call site unwrap it.
    """

    shas: dict[str, str]


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    """Stub the DB-backed usage logger so the tools run fully offline."""

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def plain_vault(monkeypatch, tmp_path):
    """A vault with no git repository anywhere above it."""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(tools.settings, "vault_path", str(root))
    return root


@pytest.fixture
def vault(monkeypatch, tmp_path):
    """The constructed history. Returns the root with a `shas` mapping attached.

    Six commits, each by a named author at a pinned date:

    1. `import`   — Alice creates `Inbox/Kickoff.md` and `Notes/Stable.md`
    2. `rename`   — Alice moves it to `Projects/Kickoff.md`
    3. `decision` — Bob appends the `## Decision` section
    4. `reformat` — Carol rewrites `* ` bullets as `- ` (a real content change)
    5. `ignore`   — Alice adds `.git-blame-ignore-revs` naming commit 4
    6. `revision` — Dave edits the first bullet only
    """
    root = _Vault(tmp_path / "vault")
    root.mkdir()
    monkeypatch.setattr(tools.settings, "vault_path", str(root))

    _git(root, "init", "-q", "-b", "main", ".")
    shas: dict[str, str] = {}

    _write(root, "Inbox/Kickoff.md", KICKOFF_AT_IMPORT)
    _write(root, "Notes/Stable.md", "# Stable\n\nnothing changes here\n")
    _git(root, "add", "-A")
    shas["import"] = _commit(root, "import the vault", IMPORT_DATE, ALICE)

    (root / "Projects").mkdir()
    _git(root, "mv", "Inbox/Kickoff.md", "Projects/Kickoff.md")
    shas["rename"] = _commit(root, "reorganise the inbox", RENAME_DATE, ALICE)

    _write(root, "Projects/Kickoff.md", KICKOFF_AT_DECISION)
    _git(root, "add", "-A")
    shas["decision"] = _commit(root, "record the decision", DECISION_DATE, BOB)

    _write(root, "Projects/Kickoff.md", KICKOFF_AT_REFORMAT)
    _git(root, "add", "-A")
    shas["reformat"] = _commit(root, "reformat every note", REFORMAT_DATE, CAROL)

    _write(root, ".git-blame-ignore-revs", f"# the bulk reformat\n{shas['reformat']}\n")
    _git(root, "add", "-A")
    shas["ignore"] = _commit(root, "ignore the reformat in blame", IGNORE_DATE, ALICE)

    _write(root, "Projects/Kickoff.md", KICKOFF_FINAL)
    _git(root, "add", "-A")
    shas["revision"] = _commit(root, "revise the first bullet", REVISION_DATE, DAVE)

    root.shas = shas
    return root


def _blame_rows(result: str) -> dict[int, str]:
    """`{line number: the rendered row}` for a `note_blame` response."""
    rows: dict[int, str] = {}
    for line in result.splitlines():
        head, sep, _ = line.partition(" | ")
        if sep and head.strip().isdigit():
            rows[int(head.strip())] = line
    return rows


# ── note_history ─────────────────────────────────────────────────────────────


async def test_history_reports_the_birth_commit_with_its_backdated_author_date(vault):
    """The single most-asked question: when was this note created?

    The creation date is the *import* commit's authored date, four commits and
    one rename ago — not the rename, not the newest commit, and not the
    committed date.
    """
    result = await tools.note_history_impl("Projects/Kickoff.md")

    assert "**Created**" in result
    created = next(line for line in result.splitlines() if line.startswith("**Created**"))
    assert IMPORT_DATE in created
    assert "Alice Import <alice@example.invalid>" in created
    assert vault.shas["import"][:7] in created
    # ...and it names the path the note was born under, not today's path.
    assert "`Inbox/Kickoff.md`" in created


async def test_history_follows_the_rename_back_past_it(vault):
    """Without `--follow` the log would begin at the rename commit."""
    result = await tools.note_history_impl("Projects/Kickoff.md")

    for key in ("import", "rename", "decision", "reformat", "revision"):
        assert vault.shas[key][:7] in result, key
    assert "renamed from `Inbox/Kickoff.md` to `Projects/Kickoff.md`" in result


async def test_history_reports_both_timestamps_and_both_shas(vault):
    result = await tools.note_history_impl("Projects/Kickoff.md")

    assert vault.shas["revision"] in result, "the full sha is reported"
    assert f"authored {REVISION_DATE}" in result
    assert f"committed {REVISION_DATE}" in result
    assert "Dave Reviser <dave@example.invalid>" in result


async def test_history_marks_how_the_path_changed_in_each_commit(vault):
    result = await tools.note_history_impl("Projects/Kickoff.md")

    bullets = [line for line in result.splitlines() if line.startswith("- `")]
    added = next(line for line in bullets if vault.shas["import"][:7] in line)
    assert "added" in added
    modified = next(line for line in bullets if vault.shas["decision"][:7] in line)
    assert "modified" in modified
    renamed = next(line for line in bullets if vault.shas["rename"][:7] in line)
    assert "renamed" in renamed


async def test_history_limit_is_clamped_and_the_birth_survives_it(vault):
    """A `limit` short of the whole history must not cost the creation date."""
    result = await tools.note_history_impl("Projects/Kickoff.md", limit=1)

    assert "1 commit shown" in result
    bullets = [line for line in result.splitlines() if line.startswith("- `")]
    assert len(bullets) == 1
    assert vault.shas["revision"][:7] in bullets[0]
    # The birth commit is not in the list, and is still reported, because it
    # comes from its own query rather than from the tail of this one.
    assert vault.shas["import"][:7] not in bullets[0]
    created = next(line for line in result.splitlines() if line.startswith("**Created**"))
    assert IMPORT_DATE in created
    assert vault.shas["import"][:7] in created


async def test_history_of_an_untracked_note_says_so(vault):
    (vault / "Notes" / "Fresh.md").write_text("# Fresh\n", encoding="utf-8")

    result = await tools.note_history_impl("Notes/Fresh.md")

    assert "No git history for `Notes/Fresh.md`" in result
    assert "untracked" in result


async def test_history_in_a_vault_that_is_not_a_repository(plain_vault):
    (plain_vault / "Note.md").write_text("# Note\n", encoding="utf-8")

    result = await tools.note_history_impl("Note.md")

    assert "not a git repository" in result
    assert "initialise one" in result


async def test_history_refuses_a_traversal_path(vault):
    result = await tools.note_history_impl("../../../etc/passwd")

    assert "Path traversal denied" in result
    assert "commit" not in result.lower()


async def test_history_refuses_a_dot_directory(vault):
    result = await tools.note_history_impl(".git/config")

    assert "Hidden path denied" in result


async def test_history_refuses_the_vault_root_itself(vault):
    result = await tools.note_history_impl(".")

    assert "not a note" in result


# ── note_blame ───────────────────────────────────────────────────────────────


async def test_blame_attributes_lines_to_the_two_authors_that_wrote_them(vault):
    result = await tools.note_blame_impl("Projects/Kickoff.md")

    rows = _blame_rows(result)
    assert set(rows) == set(range(1, 9)), rows
    assert "Alice Import" in rows[1], "the title came from the import"
    assert IMPORT_DATE in rows[1]
    assert "Bob Decider" in rows[8], "the decision sentence is Bob's"
    assert DECISION_DATE in rows[8]
    assert DECISION in rows[8]
    assert "Dave Reviser" in rows[4], "the revised bullet is Dave's"


async def test_blame_skips_the_reformat_named_in_the_ignore_revs_file(vault):
    """The whole point of `.git-blame-ignore-revs`: a bulk reformat must not
    become the author of the vault."""
    result = await tools.note_blame_impl("Projects/Kickoff.md")

    assert ".git-blame-ignore-revs` applied" in result
    rows = _blame_rows(result)
    assert "Alice Import" in rows[5], rows[5]
    assert "Carol Formatter" not in result


async def test_without_the_ignore_revs_file_the_reformat_is_the_author(vault, monkeypatch):
    """The control. Asserting only the ignored case would pass just as happily
    if `-w` were doing the work, or if line 5 had never been reformatted."""
    monkeypatch.setattr(git_history, "ignore_revs_file", lambda root: None)

    result = await tools.note_blame_impl("Projects/Kickoff.md")

    rows = _blame_rows(result)
    assert "Carol Formatter" in rows[5], rows[5]
    assert "no `.git-blame-ignore-revs` at the vault root" in result


async def test_an_unusable_ignore_revs_file_degrades_rather_than_fails(vault):
    (vault / ".git-blame-ignore-revs").write_text(
        "this-is-not-a-commit\n", encoding="utf-8"
    )

    result = await tools.note_blame_impl("Projects/Kickoff.md")

    assert "git refused it" in result
    # The blame itself still happened, at the worse attribution.
    rows = _blame_rows(result)
    assert "Carol Formatter" in rows[5]


async def test_blame_scoped_to_a_section(vault):
    result = await tools.note_blame_impl("Projects/Kickoff.md", section="Decision")

    rows = _blame_rows(result)
    assert set(rows) == {7, 8}, rows
    assert 'section "Decision"' in result
    assert all("Bob Decider" in row for row in rows.values())


async def test_blame_of_an_unknown_section_reports_the_headings(vault):
    result = await tools.note_blame_impl("Projects/Kickoff.md", section="Nope")

    assert "not found" in result
    assert "Decision" in result


async def test_blame_scoped_to_an_explicit_line_range(vault):
    result = await tools.note_blame_impl(
        "Projects/Kickoff.md", start_line=7, end_line=8
    )

    assert set(_blame_rows(result)) == {7, 8}
    assert "lines 7–8 of 8" in result


async def test_blame_clamps_an_end_line_past_the_file(vault):
    result = await tools.note_blame_impl("Projects/Kickoff.md", start_line=7, end_line=9999)

    assert set(_blame_rows(result)) == {7, 8}


async def test_blame_refuses_a_section_together_with_a_line_range(vault):
    result = await tools.note_blame_impl(
        "Projects/Kickoff.md", section="Decision", start_line=1
    )

    assert "cannot be combined" in result


async def test_blame_refuses_a_start_line_past_the_file(vault):
    result = await tools.note_blame_impl("Projects/Kickoff.md", start_line=99)

    assert "past the end of" in result


async def test_blame_refuses_a_backwards_range(vault):
    result = await tools.note_blame_impl(
        "Projects/Kickoff.md", start_line=5, end_line=2
    )

    assert "before start_line" in result


async def test_blame_of_a_missing_note(vault):
    result = await tools.note_blame_impl("Projects/Nothing.md")

    assert "Note not found" in result
    assert "note_history" in result


async def test_blame_of_an_untracked_note_says_no_commit_authored_it(vault):
    (vault / "Notes" / "Fresh.md").write_text("# Fresh\n", encoding="utf-8")

    result = await tools.note_blame_impl("Notes/Fresh.md")

    assert "No blame information" in result


async def test_blame_caps_a_long_file_and_says_it_capped(vault, monkeypatch):
    """A pathological blame must not become the response.

    The cap is asserted through the tool rather than the constant: what
    matters is that the rendered answer stops *and* announces that it stopped,
    because a silently shortened attribution reads as a complete one.
    """
    monkeypatch.setattr(tools, "MAX_BLAME_LINES", 3)
    long_note = "".join(f"line {i}\n" for i in range(1, 200))
    (vault / "Notes" / "Long.md").write_text(long_note, encoding="utf-8")
    _git(vault, "add", "-A")
    _commit(vault, "add a long note", REVISION_DATE, DAVE)

    result = await tools.note_blame_impl("Notes/Long.md")

    assert set(_blame_rows(result)) == {1, 2, 3}
    assert "Capped at 3 lines" in result
    assert "lines 1–3 of 199" in result


async def test_blame_clips_a_very_long_line(vault, monkeypatch):
    monkeypatch.setattr(tools, "MAX_BLAME_LINE_CHARS", 20)
    (vault / "Notes" / "Wide.md").write_text("z" * 5000 + "\n", encoding="utf-8")
    _git(vault, "add", "-A")
    _commit(vault, "add a wide note", REVISION_DATE, DAVE)

    result = await tools.note_blame_impl("Notes/Wide.md")

    assert "z" * 20 + "…" in result
    assert "z" * 21 not in result


async def test_blame_response_stays_under_the_read_cap(vault, monkeypatch):
    """The shared `MAX_READ_RESPONSE_CHARS` bound applies here like everywhere."""
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 1200)
    long_note = "".join(f"line {i}\n" for i in range(1, 400))
    (vault / "Notes" / "Long.md").write_text(long_note, encoding="utf-8")
    _git(vault, "add", "-A")
    _commit(vault, "add a long note", REVISION_DATE, DAVE)

    result = await tools.note_blame_impl("Notes/Long.md")

    assert len(result) <= 1200
    assert "[TRUNCATED]" in result


async def test_blame_in_a_vault_that_is_not_a_repository(plain_vault):
    (plain_vault / "Note.md").write_text("# Note\n", encoding="utf-8")

    result = await tools.note_blame_impl("Note.md")

    assert "not a git repository" in result


async def test_blame_refuses_a_traversal_path(vault):
    result = await tools.note_blame_impl("../../../etc/passwd")

    assert "Path traversal denied" in result


async def test_blame_reports_the_section_line_range_through_frontmatter(vault):
    """Section selectors are body-relative; git numbers whole-file lines.

    A note with frontmatter is where those two disagree, and getting it wrong
    would blame the wrong lines while looking perfectly plausible.
    """
    note = "---\ntitle: Fronted\ntags: [x]\n---\n\n# Top\n\n## Later\nthe later body\n"
    (vault / "Notes" / "Fronted.md").write_text(note, encoding="utf-8")
    _git(vault, "add", "-A")
    _commit(vault, "add a fronted note", REVISION_DATE, DAVE)

    result = await tools.note_blame_impl("Notes/Fronted.md", section="Later")

    rows = _blame_rows(result)
    assert set(rows) == {8, 9}, rows
    assert "the later body" in rows[9]


# ── find_when_written ────────────────────────────────────────────────────────


async def test_the_pickaxe_finds_the_commit_that_introduced_the_text(vault):
    """The headline question: "on <datetime> you wrote <text>"."""
    result = await tools.find_when_written_impl(DECISION)

    assert vault.shas["decision"][:7] in result
    assert DECISION_DATE in result
    assert "Bob Decider <bob@example.invalid>" in result
    assert "`Projects/Kickoff.md`" in result


async def test_the_pickaxe_ignores_commits_that_merely_contain_the_text(vault):
    """`-S` selects commits that changed the *number of occurrences*.

    Three later commits touch the same file while the decision sentence sits
    in it unchanged; a grep over history would return all four.
    """
    result = await tools.find_when_written_impl(DECISION)

    assert "1 commit changed" in result
    for key in ("reformat", "ignore", "revision"):
        assert vault.shas[key][:7] not in result, key


async def test_the_pickaxe_can_be_scoped_to_one_note(vault):
    result = await tools.find_when_written_impl(DECISION, path="Projects/Kickoff.md")

    assert vault.shas["decision"][:7] in result
    assert "in `Projects/Kickoff.md`" in result


async def test_the_pickaxe_scoped_to_the_wrong_note_finds_nothing(vault):
    result = await tools.find_when_written_impl(DECISION, path="Notes/Stable.md")

    assert "No commit changed" in result


async def test_the_pickaxe_reports_no_match_for_text_never_committed(vault):
    result = await tools.find_when_written_impl("a phrase nobody ever wrote")

    assert "No commit changed" in result
    assert "exact" in result


async def test_the_pickaxe_treats_its_argument_as_a_literal_by_default(vault):
    """A regex metacharacter is a character, not a pattern, unless asked."""
    result = await tools.find_when_written_impl("we chose .* over qdrant")

    assert "No commit changed" in result


async def test_the_pickaxe_regex_flag_matches_a_pattern(vault):
    result = await tools.find_when_written_impl(
        "chose [a-z]+ over qdrant", regex=True
    )

    assert vault.shas["decision"][:7] in result
    assert "regular expression" in result


async def test_the_pickaxe_never_lets_its_argument_become_an_option(vault, tmp_path):
    """`text` is its own argv element after `-S`, so a leading dash is data.

    The same holds for anything shell-shaped: there is no shell, and no argv
    element is ever built by concatenation. Each needle below would change
    what git does if either rule were broken — `--pickaxe-regex` and `-M` are
    real git options, `$(…)`/`;` are shell, and the last one names a file that
    must not come into existence.
    """
    canary = tmp_path / "canary"
    for needle in (
        "--pickaxe-regex",
        "-M",
        "; rm -rf /",
        "$(touch nope)",
        f"--output={canary}",
    ):
        result = await tools.find_when_written_impl(needle)
        assert "No commit changed" in result, needle
    assert not canary.exists()
    assert not (vault / "nope").exists()


async def test_the_pickaxe_refuses_an_empty_needle(vault):
    result = await tools.find_when_written_impl("")

    assert "is empty" in result


async def test_the_pickaxe_refuses_an_over_long_needle(vault):
    from src.config import MAX_PICKAXE_TEXT_CHARS

    result = await tools.find_when_written_impl("x" * (MAX_PICKAXE_TEXT_CHARS + 1))

    assert "MAX_PICKAXE_TEXT_CHARS" in result
    # The refusal never echoes the argument back (#149).
    assert "xxxxxxxxxx" not in result


async def test_the_pickaxe_in_a_vault_that_is_not_a_repository(plain_vault):
    result = await tools.find_when_written_impl("anything")

    assert "not a git repository" in result


async def test_the_pickaxe_refuses_a_traversal_path(vault):
    result = await tools.find_when_written_impl(DECISION, path="../../etc/passwd")

    assert "Path traversal denied" in result


async def test_the_pickaxe_response_stays_under_the_read_cap(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 900)

    for i in range(12):
        (vault / "Notes" / f"Bulk{i}.md").write_text(
            f"repeated needle {i}\n", encoding="utf-8"
        )
        _git(vault, "add", "-A")
        _commit(vault, f"bulk commit {i}", REVISION_DATE, DAVE)

    result = await tools.find_when_written_impl("repeated needle")

    assert len(result) <= 900
    assert "[TRUNCATED]" in result


# ── the plumbing's own bounds ────────────────────────────────────────────────


def test_a_git_invocation_is_bounded_by_a_deadline(vault):
    """A zero deadline is the cheapest proof the deadline is consulted at all."""
    repo = git_history.resolve_repo(vault)

    with pytest.raises(git_history.GitTimeout):
        git_history._run(
            vault, [*git_history._base_args(vault, repo.toplevel), "log"], timeout=0
        )


def test_a_git_invocation_is_bounded_by_a_byte_cap(vault):
    repo = git_history.resolve_repo(vault)

    out = git_history._run(
        vault,
        [*git_history._base_args(vault, repo.toplevel), "log", "--format=%H%n%s"],
        max_bytes=16,
    )

    assert out.truncated
    assert len(out.stdout) <= 16


async def test_every_tool_reports_a_missing_git_executable(vault, monkeypatch):
    monkeypatch.setattr(git_history.shutil, "which", lambda name: None)

    for coro in (
        tools.note_history_impl("Projects/Kickoff.md"),
        tools.note_blame_impl("Projects/Kickoff.md"),
        tools.find_when_written_impl(DECISION),
    ):
        result = await coro
        assert "git is not installed" in result


async def test_a_timeout_is_reported_rather_than_raised(vault, monkeypatch):
    """A stalled git is an in-band refusal, not a protocol error."""
    monkeypatch.setattr(git_history, "GIT_HISTORY_TIMEOUT_SECONDS", 0)

    result = await tools.note_history_impl("Projects/Kickoff.md")

    assert "did not finish within" in result


def test_the_ignore_revs_helper_refuses_a_symlink(vault, tmp_path):
    """The name is a constant, so a symlink under it is somebody aiming the
    read somewhere the vault owner never named."""
    (vault / ".git-blame-ignore-revs").unlink()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x\n", encoding="utf-8")
    (vault / ".git-blame-ignore-revs").symlink_to(outside)

    assert git_history.ignore_revs_file(vault) is None


def test_a_vault_inside_a_larger_repository_reports_vault_relative_paths(
    monkeypatch, tmp_path
):
    """`prefix` is what turns a repository-relative path back into a
    vault-relative one, and it is only exercised by this layout."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q", "-b", "main", ".")
    _write(outer, "vault/Notes/Inner.md", "# Inner\n")
    _write(outer, "outside.md", "# Outside\n")
    _git(outer, "add", "-A")
    _commit(outer, "seed", IMPORT_DATE, ALICE)

    root = outer / "vault"
    monkeypatch.setattr(tools.settings, "vault_path", str(root))
    repo = git_history.resolve_repo(root)

    assert repo.prefix == "vault/"
    assert tools._repo_relative(repo, "Notes/Inner.md") == "vault/Notes/Inner.md"
    assert tools._vault_relative(repo, "vault/Notes/Inner.md") == "Notes/Inner.md"
    assert "outside the vault" in tools._vault_relative(repo, "outside.md")
