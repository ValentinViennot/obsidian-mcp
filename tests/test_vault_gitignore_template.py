"""`deploy/vault.gitignore`: does it exclude what its comments claim?

An ignore template is a file of assertions about git's pattern semantics, and
those semantics are famously easy to get subtly wrong — `**/` matching the root
too, a negation that cannot re-include a file under an excluded directory,
`*.db` anchored somewhere other than where it was meant. Every claim the
template makes is checked here against real `git check-ignore`, on a synthetic
vault built in `tmp_path`.

The size rules are the ones that matter most: a plugin cache committed once is
in the pack files for ever, and getting it out means rewriting history in both
clones.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "vault.gitignore"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed on this host"
)


@pytest.fixture
def vault(tmp_path):
    """An empty repository carrying the template as its `.gitignore`."""
    root = tmp_path / "vault"
    root.mkdir()
    subprocess.run(
        ["git", "-C", str(root), "init", "-q", "-b", "main"],
        check=True, capture_output=True,
    )
    shutil.copyfile(TEMPLATE, root / ".gitignore")
    return root


def write(root, rel, content="x\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def ignored(root, rel):
    """`git check-ignore` — git's own answer, not a reimplementation of it."""
    result = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", "--no-index", rel],
        check=False, capture_output=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def untracked(root):
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {line[3:] for line in out.splitlines() if line.startswith("??")}


# ── What must stay OUT ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        # 1. Per-machine editor state. Rewritten many times a minute while
        # somebody is working, and meaningless on the other machine.
        ".obsidian/workspace.json",
        ".obsidian/workspace-mobile.json",
        # 2. The soft-delete bin, which `delete_note` renames into.
        ".trash/Doomed-20240101-000000.md",
        ".trash/nested/Deeper.md",
        # 3. Filesystem litter.
        ".DS_Store",
        "Projects/.DS_Store",
        "._Alpha.md",
        "Thumbs.db",
        "desktop.ini",
        # 4. The size rules: plugin caches and generated indexes.
        ".obsidian/plugins/dataview/cache/blob.json",
        ".obsidian/plugins/dataview/.cache/blob.json",
        ".obsidian/plugins/omnisearch/caches/shard-0",
        ".obsidian/plugins/some-plugin/tmp/scratch",
        ".obsidian/plugins/some-plugin/temp/scratch",
        ".obsidian/plugins/omnisearch/index/segment-0",
        ".obsidian/plugins/omnisearch/indexes/segment-0",
        ".obsidian/plugins/omnisearch/minisearch.index",
        ".obsidian/plugins/omnisearch/minisearch.idx",
        ".obsidian/plugins/some-plugin/store.db",
        ".obsidian/plugins/some-plugin/store.sqlite",
        ".obsidian/plugins/some-plugin/store.sqlite3",
        ".obsidian/plugins/deep/nested/deeper/cache/x",
        # Embedding and vector stores, wherever they were put.
        "notes.ajson",
        "some/deep/path/shard-000.ajson",
        "embeddings/vectors.bin",
        "Projects/embeddings/vectors.bin",
        "model.embeddings.json",
        "model.vectors.json",
        ".smart-env/multi/shard.ajson",
        ".smart-connections/embeddings.json",
        "copilot-index/segment",
        ".obsidian/cache",
        "Drawings/Excalidraw/Scripts/Downloaded/script.md",
    ],
)
def test_the_template_excludes_what_it_says_it_does(vault, rel):
    write(vault, rel)
    assert ignored(vault, rel), f"{rel} should be ignored by deploy/vault.gitignore"


def test_a_nested_vaults_configuration_is_excluded(vault):
    """A vault inside a vault brings its own `.obsidian/`, with its own churn."""
    write(vault, "Archive/2019/.obsidian/workspace.json")
    write(vault, "Archive/2019/.obsidian/app.json")
    assert ignored(vault, "Archive/2019/.obsidian/app.json")
    assert ignored(vault, "Archive/2019/.obsidian/workspace.json")


# ── What must stay IN ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        # Notes, obviously — including ones whose names brush against the rules.
        "Alpha.md",
        "Projects/Roadmap.md",
        "Projects/database.md",
        "Notes/index.md",
        "Notes/cache-invalidation.md",
        "Attachments/diagram.png",
        # THIS vault's own configuration is shared and worth versioning: every
        # `.obsidian/` file except the two workspace ones.
        ".obsidian/app.json",
        ".obsidian/appearance.json",
        ".obsidian/hotkeys.json",
        ".obsidian/community-plugins.json",
        ".obsidian/graph.json",
        ".obsidian/themes/Minimal/theme.css",
        # A plugin's *settings* are small and worth versioning; only its
        # derived data is not.
        ".obsidian/plugins/dataview/data.json",
        ".obsidian/plugins/dataview/manifest.json",
        ".obsidian/plugins/dataview/main.js",
        ".obsidian/plugins/dataview/styles.css",
        # An Excalidraw drawing is content. Only the plugin's *downloaded*
        # scripts are excluded.
        "Drawings/Sketch.excalidraw.md",
    ],
)
def test_the_template_keeps_what_belongs_in_the_repository(vault, rel):
    write(vault, rel)
    assert not ignored(vault, rel), f"{rel} should NOT be ignored"


def test_the_root_obsidian_directory_survives_the_nested_vault_rule(vault):
    """The negation actually re-includes it.

    `**/.obsidian/` matches the root one too, and "a file cannot be re-included
    if its parent directory is excluded" makes this the rule most likely to
    quietly drop the whole of this vault's configuration. Checked against
    `git status`, not just `check-ignore`, because the failure is git declining
    to *descend* into the directory at all.
    """
    write(vault, ".obsidian/app.json")
    write(vault, ".obsidian/workspace.json")
    write(vault, "Archive/.obsidian/app.json")
    write(vault, "Alpha.md")

    seen = untracked(vault)

    assert ".obsidian/app.json" in seen
    assert "Alpha.md" in seen
    assert ".obsidian/workspace.json" not in seen
    assert not any(name.startswith("Archive/") for name in seen)


def test_a_realistic_vault_stages_only_its_content(vault):
    """The whole template, exercised the way the sweep exercises it."""
    for rel in (
        "Alpha.md",
        "Projects/Roadmap.md",
        ".obsidian/app.json",
        ".obsidian/plugins/dataview/data.json",
    ):
        write(vault, rel)
    for rel in (
        ".obsidian/workspace.json",
        ".trash/Doomed-20240101-000000.md",
        ".DS_Store",
        ".obsidian/plugins/omnisearch/cache/segment-0",
        ".smart-env/multi/shard-000.ajson",
        "Archive/2019/.obsidian/workspace.json",
    ):
        write(vault, rel)

    subprocess.run(["git", "-C", str(vault), "add", "-A"], check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "-C", str(vault), "diff", "--cached", "--name-only"],
        check=True, capture_output=True, text=True,
    ).stdout.split()

    assert sorted(staged) == sorted(
        [
            ".gitignore",
            ".obsidian/app.json",
            ".obsidian/plugins/dataview/data.json",
            "Alpha.md",
            "Projects/Roadmap.md",
        ]
    )


def test_the_template_is_not_accidentally_empty():
    """A template that got truncated would ignore nothing and say nothing."""
    text = TEMPLATE.read_text(encoding="utf-8")
    rules = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(rules) > 25
    # Every claim the tests above make has a rule behind it.
    assert ".trash/" in rules
    assert ".obsidian/workspace.json" in rules
    assert "**/.obsidian/" in rules
    assert "!/.obsidian/" in rules
