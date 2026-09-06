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


def _describe_maturity(count: int, total: int, noun: str) -> str:
    """Say honestly how established a practice is.

    The failure mode this guards against: an agent reads "notes use frontmatter",
    adds frontmatter everywhere, and makes a 3%-consistent vault 100%
    inconsistent with its own past. Percentages, and an explicit instruction,
    prevent that.
    """
    pct = 100.0 * count / total if total else 0.0
    if pct >= 80:
        return f"**established** — {pct:.0f}% of notes have {noun}. Follow it."
    if pct >= 30:
        return (
            f"**partial** — {pct:.0f}% of notes have {noun}. Match the local "
            "neighbourhood rather than applying it globally."
        )
    return (
        f"**not a convention** — only {pct:.0f}% of notes have {noun}. "
        f"Do NOT start adding {noun} to notes that lack it, and do not treat "
        "the few existing examples as a schema to conform to."
    )


def render_guide(p: VaultProfile) -> str:
    out: list[str] = []
    add = out.append

    add("# Vault guide for AI agents")
    add("")
    add(
        "This file is served to every agent that connects to this vault through "
        "the MCP server. It describes how this vault is actually organised — "
        "measured from its contents, not aspirational. Follow it."
    )
    add("")
    add(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} from "
        f"{p.total_notes} notes. Regenerate with `scripts/generate_vault_guide.py` "
        "after any large reorganisation._"
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
        f"{p.total_notes} notes across {len(p.folders)} top-level locations. "
        "**Put a new note in the folder its subject belongs to; do not default "
        "to the vault root.**"
    )
    add("")
    add("| Location | Notes | |")
    add("|---|---:|---|")
    for name, count in p.largest_folders:
        share = 100.0 * count / p.total_notes if p.total_notes else 0
        note = ""
        if name in p.nested_vaults:
            note = "nested vault — see below"
        elif name == "(root)":
            note = "unfiled; treat as an inbox, not a destination"
        add(f"| `{name}` | {count} | {note or f'{share:.0f}% of notes'} |")
    add("")
    add(
        "Before creating a note for an existing project or topic, **check "
        "whether a folder for it already exists** and use that folder's own "
        "naming. Folder names and any short codes used inside filenames do not "
        "always match each other — search for both before inventing either."
    )
    add("")

    if p.nested_vaults:
        add("### Nested vaults")
        add("")
        for name in p.nested_vaults:
            count = p.folders.get(name, 0)
            add(
                f"- `{name}` ({count} notes) contains its own `.obsidian/` "
                "directory, so Obsidian treats it as a **separate vault**. Its "
                "notes are indexed and searchable here, but its conventions are "
                "its own. Do not reorganise it, and do not assume links resolve "
                "across the boundary."
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
        add("Date-prefix conventions in use, most common first:")
        add("")
        add("| Shape | Notes using it |")
        add("|---|---:|")
        for label, count in p.filename_patterns.most_common():
            add(f"| `{label}` | {count} |")
        add("")
        dominant = p.filename_patterns.most_common(1)[0][0]
        add(
            f"For a new dated note, use `{dominant}`. Several older shapes "
            "coexist; do not propagate them, and do not rename existing notes "
            "to match — renaming breaks inbound links."
        )
    add("")
    if p.total_notes:
        add(
            f"{p.undated_names} notes "
            f"({100.0 * p.undated_names / p.total_notes:.0f}%) have no date "
            "prefix. Evergreen or reference notes should be titled by subject, "
            "with no date."
        )
    add("")
    add("Never create a note called `Untitled`, `Untitled 1`, or similar.")
    add("")

    # ---- Frontmatter ------------------------------------------------------
    add("## Frontmatter")
    add("")
    add(_describe_maturity(p.frontmatter_notes, p.total_notes, "YAML frontmatter"))
    add("")
    if p.frontmatter_keys:
        common = ", ".join(f"`{k}`" for k, _ in p.frontmatter_keys.most_common(10))
        add(f"Keys that appear at all: {common}.")
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
    total_tag_uses = sum(p.inline_tags.values())
    # Judge by the share of notes that carry a tag, not by raw uses: one heavily
    # tagged note does not make tagging a vault-wide convention. Same measure
    # used for frontmatter, so the two sections stay comparable.
    tagged_pct = 100.0 * p.notes_with_tags / p.total_notes if p.total_notes else 0.0
    if tagged_pct < 20:
        add(
            f"**Not a convention** — only {p.notes_with_tags} of {p.total_notes} "
            f"notes ({tagged_pct:.0f}%) carry an inline tag. Do not build a "
            "tagging scheme, and do not add tags to notes as a side effect of "
            "editing them."
        )
    else:
        add(
            f"{tagged_pct:.0f}% of notes carry inline tags "
            f"({total_tag_uses} uses). Existing vocabulary:"
        )
        add("")
        add(", ".join(f"`#{t}`" for t, _ in genuine) or "_none in common use_")
        add("")
        add("Reuse an existing tag rather than coining a near-duplicate.")
    add("")

    # ---- Links ------------------------------------------------------------
    add("## Links")
    add("")
    add(
        f"Wikilinks are a core convention here: {p.link_instances} links across "
        f"{p.notes_with_links} notes ({p.linked_pct:.0f}% of the vault). "
        "**Link by bare title** — `[[Note Title]]` — not by path; that is how "
        "the existing links are written and how Obsidian resolves them."
    )
    add("")
    if p.hubs:
        add(
            "These notes are **hubs**: many others link into them. Never rename, "
            "move or delete one without being asked explicitly — doing so breaks "
            "every inbound link at once."
        )
        add("")
        for title, count in p.hubs:
            add(f"- `[[{title}]]` — {count} inbound links")
        add("")
    add(
        f"{p.broken_targets} distinct link targets currently resolve to nothing. "
        "Broken links are normal here — they are often intentional placeholders "
        "for notes not yet written. **Do not mass-fix them**, and do not create "
        "stub notes to satisfy them unless asked."
    )
    add("")
    add(
        f"{p.orphans} notes have no inbound links. That is not a defect to "
        "correct."
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
