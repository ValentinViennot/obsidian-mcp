"""Tests for the vault guide generator.

The guide is served to every connecting agent, so its failure mode is not a
crash — it is confidently describing a convention the vault does not have. An
agent told "notes use frontmatter" on a vault where 3% do will add frontmatter
everywhere and make the vault *less* self-consistent. So most of these tests are
about the generator telling the truth about how established a practice is.

Every vault here is synthetic and built in a tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "generate_vault_guide.py"
_spec = importlib.util.spec_from_file_location("generate_vault_guide", _MODULE_PATH)
gvg = importlib.util.module_from_spec(_spec)
assert _spec.loader
sys.modules["generate_vault_guide"] = gvg
_spec.loader.exec_module(gvg)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A small synthetic vault with deliberate, checkable structure."""
    root = tmp_path / "vault"
    (root / "projects").mkdir(parents=True)
    (root / "daily").mkdir()
    (root / ".obsidian").mkdir()

    (root / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "daily", "format": "YYYY/YYYY-MM-DD"})
    )

    (root / "Hub Note.md").write_text("# Hub\n\nCentral index.\n")
    for i in range(6):
        (root / "projects" / f"2024-03-0{i+1} Project Note {i}.md").write_text(
            f"# Note {i}\n\nSee [[Hub Note]] for context.\n"
        )
    (root / "daily" / "2024-05-01 Daily.md").write_text(
        "Today I read [[Hub Note]] and [[Missing Note]].\n"
    )
    (root / "Orphan.md").write_text("Nothing links here.\n")
    (root / "Tagged.md").write_text("---\ntags: [alpha]\nstatus: active\n---\n\n#alpha work\n")
    return root


def test_profile_counts_notes_and_folders(vault):
    p = gvg.profile_vault(vault)
    assert p.total_notes == 10
    assert "projects" in p.folders
    assert p.folders["projects"] == 6
    assert p.folders["(root)"] == 3


def test_daily_note_format_is_read_from_obsidian_config(vault):
    """The app's own config beats inferring the format from filenames."""
    p = gvg.profile_vault(vault)
    assert p.daily_config["format"] == "YYYY/YYYY-MM-DD"
    assert "YYYY/YYYY-MM-DD" in gvg.render_guide(p)


def test_hub_notes_are_identified(vault):
    p = gvg.profile_vault(vault)
    assert any(title == "hub note" for title, _ in p.hubs)
    guide = gvg.render_guide(p)
    assert "hubs" in guide.lower()
    assert "Never rename, move or delete one" in guide


def test_broken_links_are_counted_but_not_treated_as_defects(vault):
    p = gvg.profile_vault(vault)
    assert p.broken_targets == 1
    guide = gvg.render_guide(p)
    assert "Do not mass-fix them" in guide


def test_orphans_are_reported_without_prescribing_a_fix(vault):
    p = gvg.profile_vault(vault)
    assert p.orphans >= 1
    assert "not a defect to correct" in gvg.render_guide(p)


def test_nested_vaults_are_detected_and_flagged(tmp_path):
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "sub" / ".obsidian").mkdir(parents=True)
    (root / "a.md").write_text("x\n")
    (root / "sub" / "b.md").write_text("y\n")

    p = gvg.profile_vault(root)
    assert p.nested_vaults == ["sub"]
    guide = gvg.render_guide(p)
    assert "separate vault" in guide
    assert "Do not reorganise it" in guide


# ---------------------------------------------------------------------------
# Honesty about how established a convention is
# ---------------------------------------------------------------------------


def test_a_thin_convention_is_reported_as_not_a_convention():
    text = gvg._describe_maturity(3, 100, "YAML frontmatter")
    assert "not a convention" in text
    assert "Do NOT start adding" in text


def test_a_partial_convention_says_match_the_neighbourhood():
    text = gvg._describe_maturity(50, 100, "YAML frontmatter")
    assert "partial" in text
    assert "neighbourhood" in text


def test_an_established_convention_says_follow_it():
    text = gvg._describe_maturity(90, 100, "YAML frontmatter")
    assert "established" in text
    assert "Follow it" in text


def test_sparse_frontmatter_does_not_become_a_schema(vault):
    """One note with frontmatter must not be presented as the vault's schema."""
    p = gvg.profile_vault(vault)
    assert p.frontmatter_pct < 30
    guide = gvg.render_guide(p)
    assert "not a convention" in guide
    assert "do not treat" in guide.lower()


def test_sparse_tags_do_not_become_a_taxonomy(vault):
    guide = gvg.render_guide(gvg.profile_vault(vault))
    assert "Do not build a tagging scheme" in guide


def test_hex_colours_in_code_are_not_counted_as_tags(tmp_path):
    """Without stripping code fences, a 'tag taxonomy' fills up with hex codes."""
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text(
        "Some prose.\n\n```css\n.a { color: #ffe6cc; }\n#region thing\n```\n\n#realtag here\n"
    )
    p = gvg.profile_vault(root)
    assert "realtag" in p.inline_tags
    assert "ffe6cc" not in p.inline_tags
    assert "region" not in p.inline_tags


# ---------------------------------------------------------------------------
# The rules that protect blame — the reason the history tools exist
# ---------------------------------------------------------------------------


def test_the_guide_leads_with_blame_preserving_edit_rules(vault):
    guide = gvg.render_guide(gvg.profile_vault(vault))
    editing = guide.index("## Editing rules")
    structure = guide.index("## Where notes live")
    assert editing < structure, "editing rules must come first"
    assert "section=" in guide
    assert "append=True" in guide
    assert "Avoid a full-content replace" in guide


def test_the_guide_warns_against_root_as_a_destination(vault):
    guide = gvg.render_guide(gvg.profile_vault(vault))
    assert "do not default" in guide.lower()
    assert "Untitled" in guide


def test_the_guide_flags_sensitivity_and_discourages_deletion(vault):
    guide = gvg.render_guide(gvg.profile_vault(vault))
    assert "private and sensitive" in guide
    assert "Deleting is rarely right" in guide


def test_an_empty_vault_does_not_crash(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    p = gvg.profile_vault(root)
    assert p.total_notes == 0
    assert gvg.render_guide(p)


def test_the_generator_never_writes_to_the_vault(vault):
    before = {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}
    gvg.render_guide(gvg.profile_vault(vault))
    after = {p: p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}
    assert before == after
