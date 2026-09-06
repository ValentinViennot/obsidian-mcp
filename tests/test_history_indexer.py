"""Version walking over a real, backdated git history (migration 025).

The fixtures build genuine repositories with `GIT_AUTHOR_DATE` backdating —
not stubbed `git log` output — because everything this module asserts is a
claim about what git actually reports: that it lists a path only in commits
that changed it, that `--first-parent` linearises a merge, that `--no-renames`
turns a rename into a delete and an add, and that a change *back* to earlier
content is reported as a change (which is the one case our own blob
deduplication has to catch).

Every note here is synthetic. No real vault is read.

The storage half of the pipeline is exercised against a fake session; the
end-to-end version, with real pgvector rows and a real point-in-time query,
lives in `tests/integration/test_temporal_embeddings_pg.py`.
"""
import datetime
import os
import subprocess
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")

from src.services import history_indexer  # noqa: E402
from src.services.history_indexer import (  # noqa: E402
    BackfillStats,
    GitError,
    VersionRef,
    _chain,
    resolve_repo,
    walk_note_versions,
)

UTC = datetime.timezone.utc


def at(month: int, day: int = 1) -> datetime.datetime:
    return datetime.datetime(2024, month, day, tzinfo=UTC)


# ── building a synthetic repository ────────────────────────────────────────


class Repo:
    """A throwaway git repository with backdated commits."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._run("init", "-q", "-b", "main", ".")
        self._run("config", "user.email", "fixture@example.invalid")
        self._run("config", "user.name", "fixture")

    def _run(self, *args: str, env: dict | None = None) -> str:
        result = subprocess.run(
            [history_indexer.GIT, "-C", self.path, *args],
            capture_output=True,
            check=True,
            env=env,
        )
        return result.stdout.decode()

    def write(self, rel: str, content: str) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)

    def remove(self, rel: str) -> None:
        os.remove(os.path.join(self.path, rel))

    def commit(self, message: str, when: datetime.datetime) -> None:
        stamp = when.isoformat()
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = stamp
        # Committer date deliberately differs from nothing here, but it is set
        # so a run is reproducible; `%aI` is what the walker reads.
        env["GIT_COMMITTER_DATE"] = stamp
        self._run("add", "-A")
        self._run("commit", "-q", "-m", message, env=env)


@pytest.fixture
def repo(tmp_path):
    return Repo(str(tmp_path / "vault"))


@pytest.fixture
def vault(repo):
    """The workstream's fixture history.

    - `alpha.md` created, then edited twice (three distinct versions, the last
      current).
    - `beta.md` created and deleted partway (one version, closed).
    - `steady.md` created once and never touched again, while four later
      commits go past it.
    """
    repo.write("alpha.md", "alpha one\n")
    repo.write("steady.md", "steady forever\n")
    repo.write("beta.md", "beta original\n")
    repo.commit("c1", at(1))

    repo.write("alpha.md", "alpha two\n")
    repo.commit("c2", at(2))

    repo.remove("beta.md")
    repo.commit("c3", at(3))

    repo.write("alpha.md", "alpha three\n")
    repo.commit("c4", at(4))

    repo.write("unrelated.txt", "not markdown\n")
    repo.commit("c5", at(5))
    return repo


async def walk(repo: Repo, **kw):
    root, prefix = await resolve_repo(repo.path)
    return await walk_note_versions(root, prefix=prefix, **kw)


# ── the walk ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distinct_versions_are_counted_per_note(vault):
    by_path = await walk(vault)

    assert set(by_path) == {"alpha.md", "steady.md", "beta.md"}
    assert len(by_path["alpha.md"]) == 3
    assert len(by_path["beta.md"]) == 1
    assert len(by_path["steady.md"]) == 1


@pytest.mark.asyncio
async def test_intervals_chain_with_no_gaps_and_no_overlaps(vault):
    """Each version ends exactly where the next begins. A gap would make a
    point-in-time query answer nothing for an instant the note certainly had
    content; an overlap would return two versions of one note at once."""
    versions = await walk(vault)
    alpha = versions["alpha.md"]

    assert [v.valid_from for v in alpha] == [at(1), at(2), at(4)]
    assert [v.valid_to for v in alpha] == [at(2), at(4), None]
    for earlier, later in zip(alpha, alpha[1:]):
        assert earlier.valid_to == later.valid_from


@pytest.mark.asyncio
async def test_the_current_version_alone_has_a_null_valid_to(vault):
    """`valid_to IS NULL` is what makes "current" queryable, so exactly one
    version per surviving note may carry it."""
    versions = await walk(vault)

    for path in ("alpha.md", "steady.md"):
        current = [v for v in versions[path] if v.valid_to is None]
        assert len(current) == 1, path
        assert current[-1] is versions[path][-1]


@pytest.mark.asyncio
async def test_a_deleted_note_has_no_current_version(vault):
    """The deletion closes the last interval. Nothing about `beta.md` may read
    as current — its text is not in the vault any more."""
    beta = (await walk(vault))["beta.md"]

    assert len(beta) == 1
    assert beta[0].valid_from == at(1)
    assert beta[0].valid_to == at(3), "the deletion commit ends the interval"
    assert all(v.valid_to is not None for v in beta)


@pytest.mark.asyncio
async def test_an_unchanged_note_is_not_duplicated_by_later_commits(vault):
    """Four commits went past `steady.md` without touching it. Git reports a
    path only in the commits that changed it, which is the first half of the
    deduplication; the second half is below."""
    steady = (await walk(vault))["steady.md"]

    assert len(steady) == 1
    assert steady[0].valid_from == at(1)
    assert steady[0].valid_to is None


@pytest.mark.asyncio
async def test_a_change_back_to_earlier_content_is_deduplicated(repo):
    """The case git does *not* collapse for us. A revert (or a round-tripped
    formatter) is a real change to a real commit, so `git log` lists it — and
    embedding it would store the same bytes twice under two intervals."""
    repo.write("n.md", "first\n")
    repo.commit("c1", at(1))
    repo.write("n.md", "second\n")
    repo.commit("c2", at(2))
    repo.write("n.md", "second\n")  # touched, identical content
    repo.write("other.md", "x\n")
    repo.commit("c3", at(3))
    repo.write("n.md", "first\n")  # reverted to an *older* content
    repo.commit("c4", at(4))

    versions = (await walk(repo))["n.md"]

    # c3 changed nothing about n.md, so git emits nothing for it. c4 restores
    # older content, which is a genuine new version — it is not adjacent to the
    # identical one, so it is not a duplicate.
    assert [(v.valid_from, v.valid_to) for v in versions] == [
        (at(1), at(2)),
        (at(2), at(4)),
        (at(4), None),
    ]


@pytest.mark.asyncio
async def test_a_recreated_note_leaves_a_genuine_gap(repo):
    """Delete then re-add is not a rename and not an edit: there is an interval
    during which the note had no content, and no row may claim it."""
    repo.write("n.md", "before\n")
    repo.commit("c1", at(1))
    repo.remove("n.md")
    repo.commit("c2", at(2))
    repo.write("n.md", "after\n")
    repo.commit("c3", at(3))

    versions = (await walk(repo))["n.md"]

    assert [(v.valid_from, v.valid_to) for v in versions] == [
        (at(1), at(2)),
        (at(3), None),
    ]


@pytest.mark.asyncio
async def test_a_rename_is_a_delete_and_an_add(repo):
    """`--no-renames`, deliberately: the vault addresses notes by path, so
    after a rename the old path genuinely had no content."""
    repo.write("old.md", "content\n")
    repo.commit("c1", at(1))
    repo._run("mv", "old.md", "new.md")
    repo.commit("c2", at(2))

    versions = await walk(repo)

    assert versions["old.md"][0].valid_to == at(2)
    assert versions["new.md"][0].valid_from == at(2)
    assert versions["new.md"][0].valid_to is None


@pytest.mark.asyncio
async def test_exclude_patterns_keep_a_note_out_of_history_too(repo):
    """An excluded note must not be searchable through its past either —
    which is also why the indexer's exclusion branch deletes a note's vectors
    unscoped by validity."""
    repo.write("keep.md", "keep\n")
    repo.write("Excalidraw/drawing.md", "huge json\n")
    repo.commit("c1", at(1))

    stats = BackfillStats()
    versions = await walk(
        repo, exclude_patterns=["Excalidraw/*"], stats=stats
    )

    assert set(versions) == {"keep.md"}
    assert stats.paths_excluded == 1


@pytest.mark.asyncio
async def test_a_vault_in_a_subdirectory_yields_vault_relative_paths(tmp_path):
    """`notes_metadata.file_path` is vault-relative, so a vault living inside a
    larger repository must not come back prefixed with its own directory."""
    outer = Repo(str(tmp_path / "outer"))
    outer.write("README.md", "repo readme, not a note\n")
    outer.write("vault/note.md", "a note\n")
    outer.commit("c1", at(1))

    root, prefix = await resolve_repo(os.path.join(outer.path, "vault"))
    assert prefix == "vault/"
    versions = await walk_note_versions(root, prefix=prefix)

    assert set(versions) == {"note.md"}


@pytest.mark.asyncio
async def test_a_merge_enters_the_timeline_at_the_merge(repo):
    """`--first-parent` linearises the DAG. A validity interval cannot express
    a branch: without this, side-branch dates interleave into the mainline and
    produce two versions both claiming to be the note's text at one instant."""
    repo.write("n.md", "base\n")
    repo.commit("c1", at(1))
    repo._run("checkout", "-q", "-b", "side")
    repo.write("n.md", "from the side branch\n")
    repo.commit("side work", at(2))
    repo._run("checkout", "-q", "main")
    repo.write("other.md", "mainline\n")
    repo.commit("mainline work", at(3))

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = at(4).isoformat()
    env["GIT_COMMITTER_DATE"] = at(4).isoformat()
    repo._run("merge", "-q", "--no-ff", "-m", "merge", "side", env=env)

    versions = (await walk(repo))["n.md"]

    assert [(v.valid_from, v.valid_to) for v in versions] == [
        (at(1), at(4)),
        (at(4), None),
    ]


@pytest.mark.asyncio
async def test_a_directory_outside_a_repository_is_refused(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(GitError):
        await resolve_repo(str(plain))


# ── chaining edge cases, unit level ────────────────────────────────────────


def test_a_backwards_authored_date_cannot_invert_an_interval():
    """Authored dates are rebase- and forgery-controlled and can run backwards
    along the mainline. An interval that ends before it begins is meaningless,
    so each start is clamped to its predecessor's."""
    stats = BackfillStats()
    chained = _chain(
        [
            VersionRef("n.md", "blob-a", at(3)),
            VersionRef("n.md", "blob-b", at(1)),  # backwards
            VersionRef("n.md", "blob-c", at(5)),
        ],
        stats,
    )

    # The clamped version now starts where its predecessor did, which collapses
    # the predecessor's interval to nothing — so the predecessor is dropped and
    # what survives is a timeline that still tiles forwards.
    assert [(v.valid_from, v.valid_to) for v in chained] == [
        (at(3), at(5)),
        (at(5), None),
    ]
    assert stats.versions_dropped == 1


def test_a_version_superseded_at_the_same_instant_is_dropped_and_counted():
    """A zero-length interval is valid at no instant, so its rows could never
    be returned by any query. Dropped rather than stored — and counted, so the
    gap is a number an operator can see."""
    stats = BackfillStats()
    chained = _chain(
        [
            VersionRef("n.md", "blob-a", at(1)),
            VersionRef("n.md", "blob-b", at(1)),  # same instant
            VersionRef("n.md", "blob-c", at(2)),
        ],
        stats,
    )

    assert stats.versions_dropped == 1
    assert [(v.valid_from, v.valid_to) for v in chained] == [
        (at(1), at(2)),
        (at(2), None),
    ]


def test_the_chain_never_emits_an_inverted_or_zero_length_interval():
    stats = BackfillStats()
    chained = _chain(
        [
            VersionRef("n.md", "a", at(6)),
            VersionRef("n.md", "b", at(2)),
            VersionRef("n.md", "c", at(6)),
            VersionRef("n.md", "d", at(9)),
        ],
        stats,
    )
    for version in chained:
        if version.valid_to is not None:
            assert version.valid_to > version.valid_from


# ── the parser ─────────────────────────────────────────────────────────────


def test_the_log_parser_reads_nul_separated_raw_entries():
    """`-z` is what makes this parseable without unquoting: git's default raw
    output octal-escapes any path with a non-ASCII byte, and a vault is full of
    those."""
    stream = (
        "\x01" + "a" * 40 + "\x1f2024-01-01T00:00:00Z\x00\n"
        ":000000 100644 " + "0" * 40 + " " + "b" * 40 + " A\x00accénts é.md\x00"
        "\x01" + "c" * 40 + "\x1f2024-02-01T00:00:00+02:00\x00\n"
        ":100644 000000 " + "b" * 40 + " " + "0" * 40 + " D\x00accénts é.md\x00"
    )

    parsed = history_indexer.parse_git_log(stream)

    assert [(p[1], p[2]) for p in parsed] == [
        ("A", "accénts é.md"),
        ("D", "accénts é.md"),
    ]
    assert parsed[0][0] == at(1)
    assert parsed[1][0].utcoffset() == datetime.timedelta(hours=2)


def test_the_parser_ignores_a_commit_with_no_changes():
    stream = "\x01" + "a" * 40 + "\x1f2024-01-01T00:00:00Z\x00\n"
    assert history_indexer.parse_git_log(stream) == []
