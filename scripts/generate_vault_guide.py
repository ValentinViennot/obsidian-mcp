#!/usr/bin/env python3
"""Generate a vault guide (``CLAUDE.md``) by reading a vault's actual structure.

``get_vault_guide()`` serves the vault's own ``CLAUDE.md`` to every agent that
connects. It is the highest-leverage artefact in the system: it is what stops a
write-capable agent from scattering files into the wrong folders, inventing a
frontmatter schema, or breaking the link graph. It has to describe the vault
that exists, not an idealised one.

So this generator *measures* rather than assumes. It counts how many notes
actually carry frontmatter before telling an agent that frontmatter is a
convention; it reads the daily-note format out of Obsidian's own config rather
than guessing; it finds the notes that many others link to and marks them as
load-bearing. Where a practice is thin or inconsistent, the guide says so
plainly instead of promoting it to a rule — an agent told "always add
frontmatter" on a vault where 3% of notes have it will make the vault *less*
consistent, not more.

**Privacy**: the generator reads a private vault and writes a guide that
necessarily contains folder names and hub-note titles. That output is vault
content. Write it to the vault or to a directory outside version control —
never into this repository, which is public. The generator itself, and its
tests, contain only synthetic examples.

Usage::

    python scripts/generate_vault_guide.py --vault /path/to/vault --out CLAUDE.md
    python scripts/generate_vault_guide.py --vault /path/to/vault --stats-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {".git", ".obsidian", ".smart-env", ".trash", "node_modules"}

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
INLINE_TAG = re.compile(r"(?<![\w&#])#([A-Za-z][\w/-]{1,40})")
FM_KEY = re.compile(r"^([A-Za-z][\w-]*)\s*:", re.MULTILINE)

# Filename shapes, expressed as (regex, human-readable synthetic template).
FILENAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}"), "YYYY-MM-DD ..."),
    (re.compile(r"^\d{4}-\d{2}(?!\d)"), "YYYY-MM ..."),
    (re.compile(r"^\d{8}\s"), "YYYYMMDD ..."),
    (re.compile(r"^\d{6}\s"), "YYMMDD ..."),
    (re.compile(r"^\d{4}\s"), "YYMM ..."),
]


@dataclass
class VaultProfile:
    root: Path
    total_notes: int = 0
    total_files: int = 0
    folders: dict[str, int] = field(default_factory=dict)
    nested_vaults: list[str] = field(default_factory=list)
    filename_patterns: Counter = field(default_factory=Counter)
    undated_names: int = 0
    frontmatter_notes: int = 0
    frontmatter_keys: Counter = field(default_factory=Counter)
    inline_tags: Counter = field(default_factory=Counter)
    notes_with_tags: int = 0
    frontmatter_tags: Counter = field(default_factory=Counter)
    link_instances: int = 0
    notes_with_links: int = 0
    link_targets: Counter = field(default_factory=Counter)
    broken_targets: int = 0
    orphans: int = 0
    hubs: list[tuple[str, int]] = field(default_factory=list)
    daily_config: dict | None = None
    largest_folders: list[tuple[str, int]] = field(default_factory=list)

    #: The date this profile was measured, carried on the profile rather than
    #: read from the clock inside `render_guide`. Two reasons, and the second
    #: is the one that matters: it makes the renderer a pure function of the
    #: profile, so a test can assert the exact string a guide will contain
    #: instead of matching a date that changes at midnight; and it means every
    #: "as of" in the output names the moment the vault was *scanned*, not the
    #: moment the markdown was assembled.
    scanned_on: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    @property
    def frontmatter_pct(self) -> float:
        return 100.0 * self.frontmatter_notes / self.total_notes if self.total_notes else 0.0

    @property
    def linked_pct(self) -> float:
        return 100.0 * self.notes_with_links / self.total_notes if self.total_notes else 0.0


def _read_daily_config(vault: Path) -> dict | None:
    """Obsidian records the authoritative daily-note path format in its config.

    Reading it is strictly better than inferring the format from filenames: the
    config is what the app will actually use when it creates tomorrow's note.
    """
    path = vault / ".obsidian" / "daily-notes.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _classify_filename(stem: str) -> str | None:
    for pattern, label in FILENAME_PATTERNS:
        if pattern.match(stem):
            return label
    return None


def _scan_frontmatter(text: str) -> list[str]:
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    return FM_KEY.findall(text[3:end])


def _frontmatter_tags(text: str) -> list[str]:
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    block = text[3:end]
    match = re.search(r"^tags\s*:\s*(.*)$", block, re.MULTILINE)
    if not match:
        return []
    inline = match.group(1).strip()
    if inline.startswith("["):
        return [t.strip().strip("\"'#") for t in inline.strip("[]").split(",") if t.strip()]
    tags = []
    for line in block[match.end() :].splitlines():
        if not line.startswith((" ", "\t", "-")):
            break
        item = line.strip().lstrip("-").strip().strip("\"'#")
        if item:
            tags.append(item)
    return tags


def profile_vault(vault: Path) -> VaultProfile:
    profile = VaultProfile(root=vault, daily_config=_read_daily_config(vault))
    folder_counts: Counter = Counter()
    stems: set[str] = set()
    outbound: dict[str, set[str]] = defaultdict(set)

    for path in sorted(vault.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(vault).parts[:-1]):
            continue
        if path.is_dir():
            if (path / ".obsidian").is_dir() and path != vault:
                profile.nested_vaults.append(str(path.relative_to(vault)))
            continue
        if path.name.startswith(".") or path.parent.name in SKIP_DIRS:
            continue
        profile.total_files += 1
        if path.suffix.lower() != ".md":
            continue

        rel = path.relative_to(vault)
        profile.total_notes += 1
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        folder_counts[top] += 1
        stems.add(path.stem.casefold())

        label = _classify_filename(path.stem)
        if label:
            profile.filename_patterns[label] += 1
        else:
            profile.undated_names += 1

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        keys = _scan_frontmatter(text)
        if keys:
            profile.frontmatter_notes += 1
            profile.frontmatter_keys.update(keys)
            profile.frontmatter_tags.update(_frontmatter_tags(text))

        # Tags are counted outside fenced code, where '#' is a comment or a
        # colour, not a tag. Skipping this produces a "taxonomy" of hex codes.
        body = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        found_tags = INLINE_TAG.findall(body)
        if found_tags:
            profile.notes_with_tags += 1
        profile.inline_tags.update(found_tags)

        links = WIKILINK.findall(body)
        if links:
            profile.notes_with_links += 1
            profile.link_instances += len(links)
        for target in links:
            cleaned = target.strip().split("/")[-1].casefold()
            profile.link_targets[cleaned] += 1
            outbound[path.stem.casefold()].add(cleaned)

    profile.folders = dict(folder_counts.most_common())
    profile.largest_folders = folder_counts.most_common(25)
    profile.broken_targets = sum(1 for t in profile.link_targets if t not in stems)
    linked_to = set(profile.link_targets)
    profile.orphans = sum(1 for s in stems if s not in linked_to)
    profile.hubs = [
        (target, count)
        for target, count in profile.link_targets.most_common(12)
        if target in stems and count >= 5
    ]
    return profile


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _describe_maturity(count: int, total: int, noun: str, p: VaultProfile) -> str:
    """Say honestly how established a practice is.

    The failure mode this guards against: an agent reads "notes use frontmatter",
    adds frontmatter everywhere, and makes a 3%-consistent vault 100%
    inconsistent with its own past. The verdict, and an explicit instruction,
    prevent that.

    **The instruction leads and the percentage trails**, in parentheses and
    dated. The verdict is the durable part — a vault where frontmatter is not
    a convention does not become one by drifting a few points — while the
    number behind it is a measurement like any other here and must not read as
    current fact.
    """
    pct = 100.0 * count / total if total else 0.0
    measured = f"({pct:.0f}% of notes had {noun} {_as_of(p)}.)"
    if pct >= 80:
        return f"**Established — follow it.** {measured}"
    if pct >= 30:
        return (
            "**Partial — match the local neighbourhood rather than applying it "
            f"globally.** {measured}"
        )
    return (
        f"**Not a convention.** Do NOT start adding {noun} to notes that lack "
        "it, and do not treat the few existing examples as a schema to conform "
        f"to. {measured}"
    )


def _as_of(p: VaultProfile) -> str:
    """The one phrase every measured number carries.

    Stated identically everywhere so a reader learns it once and then knows,
    at a glance, which sentences in this guide are allowed to be out of date.
    """
    return f"at the {p.scanned_on} scan"


def render_guide(p: VaultProfile) -> str:
    """Render the guide, keeping durable rules and perishable counts apart.

    **Why the separation is the design.** The first version of this guide
    stated every measurement as a present-tense fact: "1222 notes", "108
    unfiled", "3854 links", "92 inbound links". Every one of those was true on
    the day it was generated and false soon after — the vault was reorganised
    a day later and the root went from 108 notes to 6, while the guide went on
    asserting 108 in a confident voice to every agent that connected. A number
    an agent cannot tell is stale is worse than no number, because it will be
    trusted and acted on.

    The rules, meanwhile, did not rot at all. "The root is an inbox, file
    things out of it" is as true at 6 notes as at 108. "Do not invent a
    frontmatter schema" survives any amount of reorganisation. So the two are
    now visibly different kinds of sentence: rules are stated plainly and
    unconditionally, and every measured quantity carries `_as_of()` and lives
    where a reader expects something perishable.

    The header says which is which, and says that the vault wins — an agent
    that finds this guide disagreeing with `list_files` should believe the
    vault and say so, rather than trying to reconcile the two.
    """
    out: list[str] = []
    add = out.append

    add("# Vault guide for AI agents")
    add("")
    add(
        "This file is served to every agent that connects to this vault "
        "through the MCP server. It describes how this vault is actually "
        "organised — measured from its contents, not aspirational. Follow it."
    )
    add("")
    add("## How to read this guide")
    add("")
    add(
        "It holds two kinds of statement, and they age differently:"
    )
    add("")
    add(
        "- **Rules** — where a new note goes, how to name it, what not to "
        "invent, what not to break. These are stated plainly and do not "
        "expire. Follow them."
    )
    add(
        f"- **Measurements** — every count, percentage and list marked *{_as_of(p)}*. "
        "These were true when the vault was last profiled and drift from that "
        "moment onward. Read them as orientation, never as current fact."
    )
    add("")
    add(
        "**Where this guide and the vault disagree, the vault wins.** The "
        "tools see the vault as it is now: `list_files` for what is on disk, "
        "`list_notes` for what is indexed, `get_backlinks` for who links to "
        "what. If a number here is contradicted by a tool, trust the tool, "
        "carry on with the task, and mention the discrepancy to the vault "
        "owner so the guide can be regenerated. Do not reorganise anything to "
        "make the vault match this file."
    )
    add("")
    add(
        f"_Profiled {p.scanned_on}. Regenerate with "
        "`scripts/generate_vault_guide.py` after any large reorganisation._"
    )
    add("")

    # ---- The rules that protect history -----------------------------------
    add("## Editing rules (read this first)")
    add("")
    add(
        "This vault is a git repository, and per-line authorship is a feature: "
        "the `note_blame` and `find_when_written` tools answer *when* something "
        "was written and *by whom*. How you edit determines whether those "
        "answers stay true."
    )
    add("")
    add("- **Prefer `edit_note(path, section=\"Some Heading\", ...)`.** Editing one "
        "section rewrites only those lines, so blame for the rest of the note "
        "survives.")
    add("- **Prefer `append=True`** when adding to a note. Appending never "
        "rewrites existing lines.")
    add("- **Avoid a full-content replace.** It rewrites every line, so every "
        "line's authorship becomes you, today — destroying the record of what "
        "the human wrote and when. Use it only when genuinely rewriting a whole "
        "note.")
    add("- Make one logical change per write. The commit message records the "
        "tool and the caller, so small coherent writes produce a readable "
        "history.")
    add("")

    # ---- Structure --------------------------------------------------------
    add("## Where notes live")
    add("")
    add(
        "**Put a new note in the folder its subject belongs to; do not default "
        "to the vault root.** The root is an inbox, not a destination — that "
        "holds whether it currently contains six notes or six hundred."
    )
    add("")
    add(
        "Before creating a note for an existing project or topic, **check "
        "whether a folder for it already exists** and use that folder's own "
        "naming. `list_files` shows the folders that exist right now, which is "
        "the authority; the table below is orientation. Folder names and any "
        "short codes used inside filenames do not always match each other — "
        "search for both before inventing either."
    )
    add("")
    add(f"Top-level locations, {_as_of(p)}:")
    add("")
    add(f"| Location | Notes ({p.scanned_on}) | |")
    add("|---|---:|---|")
    for name, count in p.largest_folders:
        note = ""
        if name in p.nested_vaults:
            note = "nested vault — see below"
        elif name == "(root)":
            note = "inbox; file notes out of here, do not add to it"
        add(f"| `{name}` | {count} | {note} |")
    add("")

    if p.nested_vaults:
        add("### Nested vaults")
        add("")
        for name in p.nested_vaults:
            add(
                f"- `{name}` contains its own `.obsidian/` directory, so "
                "Obsidian treats it as a **separate vault**. Its notes are "
                "indexed and searchable here, but its conventions are its own. "
                "Do not reorganise it, and do not assume links resolve across "
                "the boundary."
            )
        add("")

    # ---- Naming -----------------------------------------------------------
    add("## Naming a new note")
    add("")
    if p.daily_config and p.daily_config.get("format"):
        add(
            f"**Daily notes** are created by Obsidian at `"
            f"{p.daily_config.get('folder', '')}/{p.daily_config['format']}.md` "
            "(this is read from the vault's own daily-notes config, so it is "
            "authoritative). Never hand-create a daily note at a different path "
            "— it will not be found by the app."
        )
        add("")
    if p.filename_patterns:
        dominant = p.filename_patterns.most_common(1)[0][0]
        add(
            f"**For a new dated note, use `{dominant}`.** Several older shapes "
            "coexist; do not propagate them, and do not rename existing notes "
            "to match — renaming breaks inbound links."
        )
        add("")
        add(f"Shapes observed, most common first, {_as_of(p)}:")
        add("")
        add(f"| Shape | Notes ({p.scanned_on}) |")
        add("|---|---:|")
        for label, count in p.filename_patterns.most_common():
            add(f"| `{label}` | {count} |")
        add("")
    add(
        "**Evergreen or reference notes should be titled by subject, with no "
        "date prefix** — that is the majority of this vault."
    )
    add("")
    add("Never create a note called `Untitled`, `Untitled 1`, or similar.")
    add("")

    # ---- Frontmatter ------------------------------------------------------
    add("## Frontmatter")
    add("")
    add(_describe_maturity(p.frontmatter_notes, p.total_notes, "YAML frontmatter", p))
    add("")
    if p.frontmatter_keys:
        common = ", ".join(f"`{k}`" for k, _ in p.frontmatter_keys.most_common(10))
        add(f"Keys seen at all, {_as_of(p)}: {common}.")
        add("")
        if p.frontmatter_pct < 30:
            add(
                "Most of these come from a handful of notes or are written by "
                "plugins. Treat them as evidence of past experiments, not as a "
                "schema. If you are asked to introduce a consistent schema, "
                "propose it to the vault owner first — do not roll it out "
                "unilaterally."
            )
            add("")

    # ---- Tags -------------------------------------------------------------
    add("## Tags")
    add("")
    genuine = [(t, c) for t, c in p.inline_tags.most_common(20) if c >= 2]
    tagged_pct = 100.0 * p.notes_with_tags / p.total_notes if p.total_notes else 0.0
    if tagged_pct < 20:
        add(
            "**Not a convention.** Do not build a tagging scheme, and do not "
            "add tags to notes as a side effect of editing them. "
            f"({p.notes_with_tags} of {p.total_notes} notes — "
            f"{tagged_pct:.0f}% — carried an inline tag {_as_of(p)}.)"
        )
    else:
        add(
            "**Reuse an existing tag rather than coining a near-duplicate.** "
            f"({tagged_pct:.0f}% of notes carried inline tags {_as_of(p)}.) "
            "Vocabulary in use:"
        )
        add("")
        add(", ".join(f"`#{t}`" for t, _ in genuine) or "_none in common use_")
    add("")

    # ---- Links ------------------------------------------------------------
    add("## Links")
    add("")
    add(
        "Wikilinks are a core convention here. **Link by bare title** — "
        "`[[Note Title]]` — not by path; that is how the existing links are "
        "written and how Obsidian resolves them. A bare title keeps resolving "
        "after the note is moved to another folder, which a path-style link "
        "does not."
    )
    add("")
    add(
        f"({p.link_instances} links across {p.notes_with_links} notes, "
        f"{p.linked_pct:.0f}% of the vault, {_as_of(p)}.)"
    )
    add("")
    if p.hubs:
        add(
            "These notes are **hubs**: many others link into them. **Never "
            "rename, move or delete one without being asked explicitly** — "
            "doing so breaks every inbound link at once. Check the current "
            "count with `get_backlinks` before touching any note; a note not "
            "on this list may have become a hub since."
        )
        add("")
        add(f"Ranked by inbound links {_as_of(p)}:")
        add("")
        for title, _count in p.hubs:
            add(f"- `[[{title}]]`")
        add("")
    add(
        "**Broken links are normal here** — they are often intentional "
        "placeholders for notes not yet written. Do not mass-fix them, and do "
        "not create stub notes to satisfy them unless asked. "
        f"({p.broken_targets} distinct targets resolved to nothing {_as_of(p)}.)"
    )
    add("")
    add(
        "**A note with no inbound links is not a defect to correct.** "
        f"({p.orphans} such notes {_as_of(p)}.)"
    )
    add("")

    # ---- Safety -----------------------------------------------------------
    add("## Care")
    add("")
    add(
        "- This is a personal vault containing private and sensitive material, "
        "including financial, medical and personal-relationship notes. Read what "
        "you need for the task; do not summarise, aggregate or relocate personal "
        "content beyond what was asked."
    )
    add(
        "- **Deleting is rarely right.** Prefer leaving a note in place. Git "
        "retains history, but a delete still disappears the note from the "
        "author's view."
    )
    add(
        "- When unsure where something belongs, **ask** or put it where the "
        "most similar existing note lives. Do not invent a new top-level folder."
    )
    add("")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--out", type=Path, help="where to write the guide")
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="print the measured profile as JSON and write nothing",
    )
    args = parser.parse_args()

    if not args.vault.is_dir():
        parser.error(f"vault not found: {args.vault}")

    profile = profile_vault(args.vault)

    if args.stats_only:
        print(
            json.dumps(
                {
                    "notes": profile.total_notes,
                    "files": profile.total_files,
                    "folders": len(profile.folders),
                    "nested_vaults": len(profile.nested_vaults),
                    "frontmatter_pct": round(profile.frontmatter_pct, 1),
                    "linked_pct": round(profile.linked_pct, 1),
                    "link_instances": profile.link_instances,
                    "broken_targets": profile.broken_targets,
                    "orphans": profile.orphans,
                    "hub_count": len(profile.hubs),
                    "filename_patterns": dict(profile.filename_patterns),
                },
                indent=2,
            )
        )
        return 0

    guide = render_guide(profile)
    if args.out:
        args.out.write_text(guide, encoding="utf-8")
        print(f"wrote {len(guide)} bytes to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(guide)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
