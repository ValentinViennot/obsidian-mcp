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


# `_describe_maturity` now takes the profile too, because its measured half is
# dated like every other number in the guide. A fixed `scanned_on` keeps these
# assertions exact instead of chasing today's date.
def _profile(scanned_on: str = "2020-01-01") -> gvg.VaultProfile:
    return gvg.VaultProfile(root=Path("/nowhere"), scanned_on=scanned_on)


def test_a_thin_convention_is_reported_as_not_a_convention():
    text = gvg._describe_maturity(3, 100, "YAML frontmatter", _profile())
    assert "Not a convention" in text
    assert "Do NOT start adding" in text


def test_a_partial_convention_says_match_the_neighbourhood():
    text = gvg._describe_maturity(50, 100, "YAML frontmatter", _profile())
    assert "Partial" in text
    assert "neighbourhood" in text


def test_an_established_convention_says_follow_it():
    text = gvg._describe_maturity(90, 100, "YAML frontmatter", _profile())
    assert "Established" in text
    assert "follow it" in text.lower()


def test_the_verdict_leads_and_the_measurement_trails_it(vault):
    """The rot this guards against: a percentage stated as present-tense fact.

    The verdict is durable — a vault where frontmatter is not a convention
    does not become one by drifting a few points — so it is the sentence an
    agent reads first, and the number behind it is parenthetical and dated.
    """
    text = gvg._describe_maturity(3, 100, "YAML frontmatter", _profile("2020-01-01"))
    assert text.startswith("**Not a convention.**")
    assert "(3% of notes had YAML frontmatter at the 2020-01-01 scan.)" in text


def test_sparse_frontmatter_does_not_become_a_schema(vault):
    """One note with frontmatter must not be presented as the vault's schema."""
    p = gvg.profile_vault(vault)
    assert p.frontmatter_pct < 30
    guide = gvg.render_guide(p)
    assert "Not a convention" in guide
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


# ---------------------------------------------------------------------------
# Durable rules vs. perishable measurements
#
# The rot this section exists to prevent, from the real vault: the guide said
# "(root) | 108 | unfiled; treat as an inbox" and "1222 notes", "3854 links",
# "92 inbound links" — every one stated as present-tense fact. A day later an
# agent reorganised the vault, the root went from 108 notes to 6, and the
# guide went on asserting 108 in a confident voice to every agent that
# connected. Nothing was wrong with the *rules*; they held perfectly. What
# rotted was every sentence that had a number in it and no date on it.
# ---------------------------------------------------------------------------


def test_every_measured_number_is_dated(vault):
    """A count an agent cannot tell is stale will be trusted and acted on.

    Not an exhaustive parse — a cheap structural proxy: the guide's numeric
    claims live in table rows or in parenthetical clauses that carry the
    scan phrase. What this really pins is that the phrase is present, uniform,
    and explained, so a reader learns it once.
    """
    p = gvg.profile_vault(vault)
    p.scanned_on = "2020-01-01"
    guide = gvg.render_guide(p)
    assert "at the 2020-01-01 scan" in guide
    # Explained once, near the top, before any number relies on it.
    explanation = guide.index("These were true when the vault was last profiled")
    first_use = guide.index("at the 2020-01-01 scan")
    assert explanation < guide.index("## Editing rules")
    assert first_use < guide.index("## Editing rules")


def test_the_vault_wins_when_the_guide_disagrees(vault):
    """The instruction that makes a stale guide degrade instead of mislead."""
    guide = gvg.render_guide(gvg.profile_vault(vault))
    assert "the vault wins" in guide.lower()
    assert "trust the tool" in guide.lower()
    # And it must not tell an agent to "fix" the vault to match this file.
    assert "Do not reorganise anything to make the vault match this file" in guide


def test_the_root_rule_does_not_depend_on_the_current_count(vault):
    """"The root is an inbox" is as true at six notes as at six hundred.

    The old wording rendered the rule *inside* a table cell next to a count,
    so the sentence a reader took away was "the root has 108 notes". The rule
    now stands on its own in prose, and the table carries only the number.
    """
    guide = gvg.render_guide(gvg.profile_vault(vault))
    assert "The root is an inbox, not a destination" in guide
    assert "six notes or six hundred" in guide


def test_hub_notes_are_named_without_freezing_their_counts(tmp_path):
    """Hub *identity* is fairly durable; the inbound tally is not.

    Quoting "92 inbound links" invites an agent to reason from it. Naming the
    hubs and sending the agent to `get_backlinks` for the number keeps the
    load-bearing part and drops the part that decays.
    """
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Hub.md").write_text("hub\n", encoding="utf-8")
    for i in range(12):
        (root / f"n{i}.md").write_text("see [[Hub]]\n", encoding="utf-8")
    guide = gvg.render_guide(gvg.profile_vault(root))
    # Link targets are case-folded by the profiler, so the guide names the hub
    # the way the links spell it, not the way the file does.
    assert "`[[hub]]`" in guide.lower()
    assert "inbound links" in guide  # the concept is still explained
    assert "12 inbound links" not in guide  # the frozen tally is not
    assert "get_backlinks" in guide


def test_the_renderer_is_a_pure_function_of_the_profile(vault):
    """Same profile in, same bytes out — no clock read inside the renderer.

    This is what lets the assertions above name an exact date instead of
    matching whatever today happens to be, and it is why `scanned_on` lives on
    the profile rather than being read from `datetime.now()` mid-render.
    """
    p = gvg.profile_vault(vault)
    p.scanned_on = "1999-12-31"
    assert gvg.render_guide(p) == gvg.render_guide(p)
    assert "1999-12-31" in gvg.render_guide(p)
    assert "Profiled 1999-12-31" in gvg.render_guide(p)
