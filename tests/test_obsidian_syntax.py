"""Obsidian syntax, asserted against a synthetic fixture vault.

`tests/fixtures/obsidian_vault/` is a small vault written for this file and for
nothing else: every note in it is a deliberate specimen of one piece of
Obsidian syntax. That vault is the durable part of this change. The extractors
it exercises have each been quietly wrong at least once — a hex colour entering
the tag vocabulary, an aliased link stored as dangling, an ambiguous basename
resolving to the archived copy — and each of those failures is invisible from
inside the server: the tool answers, the answer is just missing edges or full
of noise. A fixture that pins the whole grammar at once is what stops them
coming back one at a time.

Everything here is pure-function: parse, extract, resolve. No database, no
network, no filesystem beyond reading the fixture, so the module runs on the
offline suite.
"""

import json
import os
import tempfile
from pathlib import Path

# `src.config`'s module-level `Settings()` reads `./.env` relative to CWD, and
# this repo's real `.env` is not present in the test image. Same preamble every
# other pure-function module in this suite uses.
os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
_REPO_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "obsidian_vault"
os.chdir(tempfile.gettempdir())

from src.services.embeddings import clean_at_version, clean_for_embedding  # noqa: E402
from src.services.links import (  # noqa: E402
    BODY,
    FULL_NOTE,
    build_vault_index,
    extract_links,
    mask_code_and_comments,
    normalize_aliases,
    resolve_target,
)
from src.services.vault import (  # noqa: E402
    _scan_headings,
    extract_tags,
    parse_frontmatter,
)

VAULT = _REPO_FIXTURES

# Every markdown note in the fixture vault, by the vault-relative path the
# indexer would store, paired with a stable synthetic id.
NOTE_PATHS = [
    "Archive/2019/Meeting Notes.md",
    "Archive/2019/Old Plan.md",
    "Drawings/Sketch.excalidraw.md",
    "Hub.md",
    "Projects/Chimera.md",
    "Projects/Meeting Notes.md",
    "README.md",
    "Reference/Colours.md",
    "Reference/Tasks.md",
]
NOTE_IDS = {path: i + 1 for i, path in enumerate(NOTE_PATHS)}


def _raw(rel_path: str) -> str:
    return (VAULT / rel_path).read_text(encoding="utf-8")


def _body(rel_path: str) -> str:
    """The post-frontmatter body, which is what every extractor consumes."""
    return parse_frontmatter(_raw(rel_path))[1]


def _frontmatter(rel_path: str) -> dict:
    return parse_frontmatter(_raw(rel_path))[0]


def _vault_index() -> dict:
    """The index the indexer builds: paths, ids, and each note's aliases."""
    return build_vault_index([
        (path, NOTE_IDS[path], _frontmatter(path).get("aliases"))
        for path in NOTE_PATHS
    ])


def _resolve(target: str, source: str) -> str | None:
    """`resolve_target`, reported as the note's path rather than its id."""
    note_id = resolve_target(target, source, _vault_index())
    if note_id is None:
        return None
    return next(p for p, i in NOTE_IDS.items() if i == note_id)


def _links(rel_path: str):
    return extract_links(_body(rel_path), context=BODY)


def _targets(rel_path: str, kind: str | None = None) -> list[str]:
    return [
        link.target for link in _links(rel_path)
        if kind is None or link.kind == kind
    ]


# ══════════════════════════════════════════════════════════════════════════
# The fixture is complete and stays complete
# ══════════════════════════════════════════════════════════════════════════


def test_the_fixture_vault_holds_exactly_the_notes_this_module_indexes():
    """A note added to the fixture without being added to `NOTE_PATHS` would be
    invisible to resolution here, so every assertion about ambiguity below
    would be asserting about the wrong vault."""
    on_disk = sorted(
        str(p.relative_to(VAULT)) for p in VAULT.rglob("*.md")
    )
    assert on_disk == sorted(NOTE_PATHS)


def test_the_non_markdown_specimens_exist():
    """The attachment and canvas targets are real files, so a test that says
    "this link names a file that exists" is saying something."""
    for rel in ("Assets/diagram.png", "Assets/spec.pdf", "Board.canvas"):
        assert (VAULT / rel).is_file(), rel


# ══════════════════════════════════════════════════════════════════════════
# 1. Wikilink forms: alias, heading anchor, block reference
# ══════════════════════════════════════════════════════════════════════════


def test_alias_and_anchor_do_not_change_the_target():
    """`[[X|alias]]`, `[[X#Heading]]` and `[[X#^block]]` all target `X`. This
    has always held; what is new is that the alias and anchor are *reported*
    instead of discarded, so nothing downstream has to re-split `link_text`."""
    by_text = {link.link_text: link for link in _links("Hub.md")}

    plain = by_text["[[Chimera]]"]
    assert (plain.target, plain.anchor, plain.display) == ("Chimera", "", "")

    aliased = by_text["[[Chimera|the programme]]"]
    assert aliased.target == "Chimera"
    assert aliased.display == "the programme"
    assert aliased.anchor == ""

    heading = by_text["[[Chimera#Goals]]"]
    assert heading.target == "Chimera"
    assert heading.anchor == "Goals"
    assert heading.is_block_ref is False

    block = by_text["[[Chimera#^decision-1]]"]
    assert block.target == "Chimera"
    assert block.anchor == "^decision-1"
    assert block.is_block_ref is True


def test_a_markdown_link_reports_its_anchor_the_same_way():
    """Both link grammars have to mean the same thing by `anchor`, or a caller
    that reads it has to know which kind it is holding."""
    anchored = next(
        link for link in _links("Hub.md")
        if link.kind == "markdown" and link.target.endswith("Chimera")
    )
    assert anchored.target == "Projects/Chimera"
    assert anchored.anchor == "Goals"


def test_every_anchored_and_aliased_form_resolves_to_the_same_note():
    for target in (
        "Chimera",
        "Chimera#Goals",
        "Chimera#^decision-1",
        "Chimera|the programme",
        "Projects/Chimera",
    ):
        assert _resolve(target, "Hub.md") == "Projects/Chimera.md", target


# ══════════════════════════════════════════════════════════════════════════
# 2. Embeds
# ══════════════════════════════════════════════════════════════════════════


def test_an_embed_is_a_link_of_its_own_kind():
    """`![[X]]` is an edge in the graph — a transclusion is the strongest form
    of "this note depends on that one" there is — and it is distinguishable
    from a plain reference so a caller can tell them apart."""
    embeds = [link for link in _links("Hub.md") if link.kind == "embed"]
    assert [link.link_text for link in embeds] == [
        "![[Chimera]]",
        "![[Chimera#Goals]]",
        "![[Assets/diagram.png]]",
    ]
    assert _resolve(embeds[0].target, "Hub.md") == "Projects/Chimera.md"
    # A section embed still targets the note; the section is the anchor.
    assert embeds[1].anchor == "Goals"


def test_an_attachment_embed_resolves_to_no_note_and_is_not_a_broken_link():
    """Only `.md` is indexed, so `![[Assets/diagram.png]]` can never resolve —
    however healthy the vault is. `get_links` reports these separately from
    genuinely dangling links rather than inviting an agent to "fix" them."""
    from src.mcp_server.tools import _is_attachment_target

    for target in ("Assets/diagram.png", "Assets/spec.pdf", "Board.canvas"):
        assert _resolve(target, "Hub.md") is None, target
        assert _is_attachment_target(target) is True, target
    # The counter-example that keeps the classifier honest: a missing NOTE has
    # no attachment suffix and stays a dangling link.
    assert _is_attachment_target("Nowhere In Particular") is False


# ══════════════════════════════════════════════════════════════════════════
# 3. Path style, bare names, and Obsidian's shortest-path rule
# ══════════════════════════════════════════════════════════════════════════


def test_a_path_style_link_resolves_exactly():
    assert _resolve("Projects/Meeting Notes", "Hub.md") == "Projects/Meeting Notes.md"
    assert _resolve("Archive/2019/Old Plan.md", "Hub.md") == "Archive/2019/Old Plan.md"


def test_a_bare_name_prefers_the_same_folder():
    """Obsidian's first rule, and the one this server already had."""
    assert (
        _resolve("Meeting Notes", "Projects/Chimera.md")
        == "Projects/Meeting Notes.md"
    )
    assert (
        _resolve("Meeting Notes", "Archive/2019/Old Plan.md")
        == "Archive/2019/Meeting Notes.md"
    )


def test_an_ambiguous_bare_name_takes_the_shortest_path_not_the_first_letter():
    """The regression this rule exists for.

    From `Hub.md` at the vault root neither candidate is in the source's
    folder, so the tie-break decides. `Archive/2019/Meeting Notes.md` sorts
    alphabetically BEFORE `Projects/Meeting Notes.md`, so the old rule handed
    every ambiguous `[[Meeting Notes]]` in the vault to the 2019 archive.
    Obsidian resolves the note nearest the vault root; so does this now.
    """
    assert _resolve("Meeting Notes", "Hub.md") == "Projects/Meeting Notes.md"


def test_a_bare_name_with_one_match_is_untouched_by_the_tie_break():
    assert _resolve("Old Plan", "Hub.md") == "Archive/2019/Old Plan.md"
    assert _resolve("Nowhere In Particular", "Hub.md") is None


# ══════════════════════════════════════════════════════════════════════════
# 4. Aliases
# ══════════════════════════════════════════════════════════════════════════


def test_a_link_written_through_an_alias_resolves():
    """The single largest missing-edge class. `Projects/Chimera.md` declares
    `aliases: [Chimera Programme, PRD-7]`, and both were stored as dangling
    rows before — so `get_backlinks` on the note was quietly incomplete in
    exactly the way that is impossible to notice from the answer."""
    assert _resolve("Chimera Programme", "Hub.md") == "Projects/Chimera.md"
    assert _resolve("PRD-7", "Hub.md") == "Projects/Chimera.md"


def test_alias_resolution_ignores_case():
    assert _resolve("prd-7", "Hub.md") == "Projects/Chimera.md"
    assert _resolve("CHIMERA PROGRAMME", "Hub.md") == "Projects/Chimera.md"


def test_a_scalar_alias_list_is_read_too():
    """`Hub.md` writes `aliases: Index, Start Here` — the comma-separated
    scalar form, which Obsidian accepts and which a list-only reader drops."""
    assert normalize_aliases(_frontmatter("Hub.md")["aliases"]) == [
        "Index", "Start Here",
    ]
    assert _resolve("Start Here", "Projects/Chimera.md") == "Hub.md"


def test_a_filename_always_beats_an_alias():
    """Obsidian's ordering, and the safe one: a note that IS called `X` wins
    over a note that merely calls itself `X`."""
    index = build_vault_index([
        ("Real.md", 1, None),
        ("Impostor.md", 2, ["Real"]),
    ])
    assert resolve_target("Real", "Hub.md", index) == 1


def test_the_alias_forms_obsidian_accepts():
    assert normalize_aliases(None) == []
    assert normalize_aliases(["A", "B"]) == ["A", "B"]
    assert normalize_aliases("A, B") == ["A", "B"]
    assert normalize_aliases("A\nB") == ["A", "B"]
    # A list ELEMENT is one name: a comma inside it is part of the name.
    assert normalize_aliases(["Smith, John"]) == ["Smith, John"]
    # Wrapped in wikilink brackets, as some plugins write them.
    assert normalize_aliases(["[[Wrapped]]"]) == ["Wrapped"]
    # Duplicates collapse; blanks vanish.
    assert normalize_aliases(["A", "A", "", "  "]) == ["A"]


def test_case_insensitive_filename_resolution():
    """Obsidian folds case when resolving a link. `[[chimera]]` is a working
    link there and was a dangling row here."""
    assert _resolve("chimera", "Hub.md") == "Projects/Chimera.md"
    assert _resolve("projects/meeting notes", "Hub.md") == "Projects/Meeting Notes.md"


def test_the_widened_rules_are_opt_out_for_the_destructive_path():
    """`move_note` decides what to overwrite by asking `resolve_target`, so it
    passes both flags False and its on-disk behaviour is unchanged. An alias
    and a case-differing name both survive a move on their own — the alias
    travels in the target's frontmatter, the stem is not changed by a folder
    move — so there is nothing for the rewriter to fix and everything for it to
    break."""
    index = _vault_index()
    strict = {"follow_aliases": False, "case_insensitive": False}
    assert resolve_target("Chimera Programme", "Hub.md", index, **strict) is None
    assert resolve_target("chimera", "Hub.md", index, **strict) is None
    # And the flags are actually what `move_note` passes.
    from src.mcp_server.tools import _MOVE_RESOLUTION

    assert _MOVE_RESOLUTION == strict


# ══════════════════════════════════════════════════════════════════════════
# 5. Tags
# ══════════════════════════════════════════════════════════════════════════


def _tags(rel_path: str) -> set[str]:
    return set(extract_tags(_body(rel_path), _frontmatter(rel_path)))


def test_hex_colours_are_not_tags():
    """Measured on a real vault, colour codes were the majority of the
    extracted tag vocabulary: every `#ffe6cc` in an unfenced diagram export
    was a tag, and `#d5e8d4` outranked every tag anybody had actually
    written."""
    tags = _tags("Reference/Colours.md")
    for colour in (
        "ffe6cc", "d5e8d4", "82b366", "b85450", "fff", "ccc", "000", "eeeeee",
    ):
        assert colour not in tags, colour


def test_issue_numbers_are_not_tags():
    tags = _tags("Reference/Colours.md")
    assert not {"42", "1234", "7"} & tags


def test_url_fragments_are_not_tags():
    assert not {"onboarding", "top"} & _tags("Reference/Colours.md")


def test_preprocessor_directives_in_indented_code_are_not_tags():
    """The fence recognizer deliberately leaves 4-space-indented blocks alone
    (masking them would move the offsets the write paths address by), so tag
    extraction masks them for itself. `#include`, `#define` and `#region` are
    the shapes that reached the vocabulary through that gap."""
    tags = _tags("Reference/Colours.md")
    assert not {"include", "define", "region", "endregion"} & tags


def test_a_nested_list_is_not_indented_code():
    """The false-negative this masker must not cause. A nested list item is
    indented four spaces and is the commonest construct in an Obsidian note;
    losing its tags would be a worse bug than the one being fixed."""
    tags = _tags("Reference/Colours.md")
    assert "reference/nested" in tags
    assert "reference/deep" in tags


def test_real_tags_including_unicode_and_leading_digits_survive():
    """`#([a-zA-Z][a-zA-Z0-9_/-]*)` could not start with a digit and stopped at
    the first non-ASCII character, so `#projekt/größe` entered the vocabulary
    as `projekt/gr` and `#日本語` was not a tag at all — every non-English vault
    lost most of its tags to a grammar nobody had looked at."""
    tags = _tags("Reference/Colours.md")
    assert {
        "design-system", "3d-printing", "1password", "projekt/größe",
        "日本語", "a-b/c",
    } <= tags


def test_a_frontmatter_tag_is_normalised():
    """`tags: ["#design"]` and `tags: [design]` are the same tag in Obsidian.
    Counted separately, a vault's own taxonomy splits in two."""
    tags = _tags("Reference/Colours.md")
    assert "design" in tags
    assert "#design" not in tags


def test_the_singular_frontmatter_key_is_read():
    """Obsidian accepts `tag:` as well as `tags:`; a note using it had no tags
    at all as far as `get_tags` and every `tags=` filter were concerned."""
    assert "single-key-form" in _tags("Reference/Tasks.md")


def test_a_scalar_frontmatter_tag_list_splits_on_whitespace_too():
    """`tags: project/chimera, status/active` is two tags. Splitting only on
    commas produced one tag with a leading space inside it, which no filter
    could match."""
    assert {"project/chimera", "status/active"} <= _tags("Projects/Chimera.md")
    assert {"work", "personal"} == set(extract_tags("", {"tags": "work personal"}))


def test_nested_tags_and_trailing_slashes():
    assert extract_tags("#parent/child", {}) == ["parent/child"]
    # `#project/` and `#project` are one tag in Obsidian and were two here.
    assert extract_tags("#project/ and #project", {}) == ["project"]


def test_tags_in_code_and_comments_are_not_tags():
    tags = _tags("Hub.md")
    assert "notatag" not in tags
    assert "notatagineither" not in tags
    assert "hiddentag" not in tags
    assert "alsohiddentag" not in tags
    # The note's real tags are still there.
    assert {"moc", "status/active"} <= tags


def test_a_task_or_callout_does_not_hide_its_tags():
    tags = _tags("Reference/Tasks.md")
    assert {"todo", "todo/soon"} <= tags


# ══════════════════════════════════════════════════════════════════════════
# 6. Comments
# ══════════════════════════════════════════════════════════════════════════


def test_links_inside_a_comment_are_not_graph_edges():
    """A commented-out link is not a link: Obsidian does not render it, and
    counting it made `get_backlinks` report a relationship the author had
    explicitly withdrawn."""
    targets = _targets("Hub.md")
    assert "A Ghost Note" not in targets
    assert "Also Hidden" not in targets
    # An inline comment does not swallow the rest of the note: the links after
    # it are still found.
    assert "Assets/diagram.png" in targets


def test_a_comment_hides_only_a_matched_pair():
    """An unterminated `%%` comments out the rest of the note in Obsidian. Here
    it hides nothing — a flat scanner in which one stray marker silently
    deletes every edge below it is the failure the unterminated-fence rule
    already refuses, and this one would delete graph edges rather than refuse
    a write."""
    body = "%% opened and never closed\n[[Still A Link]]\n"
    assert [link.target for link in extract_links(body)] == ["Still A Link"]


def test_a_comment_marker_inside_code_cannot_open_a_comment():
    """Code masking runs first, so a `%%` in a shell script cannot pair with a
    `%%` in the prose below it and hide everything in between."""
    body = "```sh\necho %%\n```\n[[Visible]]\n%%hidden%%\n[[Also Visible]]\n"
    assert [link.target for link in extract_links(body)] == [
        "Visible", "Also Visible",
    ]


def test_masking_preserves_offsets():
    """Every masker here is a same-length substitution, which is what makes a
    `position` reported against masked text index the original note."""
    for rel in NOTE_PATHS:
        body = _body(rel)
        assert len(mask_code_and_comments(body, context=BODY)) == len(body), rel
    raw = _raw("Hub.md")
    assert len(mask_code_and_comments(raw, context=FULL_NOTE)) == len(raw)


def test_a_comment_is_not_embedded():
    """The vector decides what `semantic_search` and `find_related` return, and
    it was being built partly from text no reader ever sees."""
    cleaned = clean_for_embedding(_body("Hub.md"))
    assert "Obsidian does not render it" not in cleaned
    assert "A Ghost Note" not in cleaned
    assert "Also Hidden" not in cleaned
    assert "hiddentag" not in cleaned
    # The visible prose survives — including the `## Hidden` heading that
    # introduces the comment, which is itself outside it.
    assert "map of content" in cleaned
    assert "## Hidden" in cleaned
    assert "aside, mid-sentence." in cleaned


# ══════════════════════════════════════════════════════════════════════════
# 7. Excalidraw and canvas
# ══════════════════════════════════════════════════════════════════════════


def test_an_excalidraw_scene_is_not_embedded():
    """An Excalidraw note parks its whole scene — element ids, coordinates,
    colours — in a `%%` block. Every drawing in a vault was contributing
    serialized geometry to vector space, which is how `find_related` on a
    drawing came back with other drawings."""
    cleaned = clean_for_embedding(_body("Drawings/Sketch.excalidraw.md"))
    assert "excalidraw" not in cleaned.lower()
    assert "appState" not in cleaned
    assert "aaaa1111" not in cleaned
    # The human-authored text elements are what remains, and they are the only
    # part of the note a person reads.
    assert "Chimera phases" in cleaned


def test_the_scene_json_does_not_become_a_tag_vocabulary():
    """`strokeColor: "#1e1e1e"` and friends are inside both a fence and a
    comment; either alone would be enough, and neither was applied before."""
    tags = _tags("Drawings/Sketch.excalidraw.md")
    assert tags == {"excalidraw"}


def test_a_heading_inside_a_comment_stays_addressable():
    """The deliberate asymmetry. Section reads and `edit_note(section=…)`
    resolve over `mask_code`, which does NOT mask comments — making a heading
    invisible to the read side while the write side still counts it is the
    destructive round-trip class this codebase has already paid for once."""
    headings = [h["text"] for h in _scan_headings(_body("Drawings/Sketch.excalidraw.md"))]
    assert headings == ["Text Elements", "Drawing"]


def test_a_canvas_file_is_json_and_is_not_a_note():
    """Nothing here parses, rewrites or indexes a `.canvas`: the indexer takes
    `.md` only, and the raw-file tools are byte transport. The assertion is
    that it is still valid JSON in the tree — i.e. that no test or tool has
    quietly started editing it."""
    data = json.loads((VAULT / "Board.canvas").read_text(encoding="utf-8"))
    assert {node["id"] for node in data["nodes"]} == {"n1", "n2", "n3"}
    assert "Board.canvas" not in NOTE_PATHS


# ══════════════════════════════════════════════════════════════════════════
# 8. Callouts, tasks, footnotes: no false positives, no lost links
# ══════════════════════════════════════════════════════════════════════════


def test_callouts_tasks_and_footnotes_produce_no_spurious_links():
    """`[!note]`, `- [ ]` and `[^src]` are all bracket forms sitting next to a
    link grammar built on brackets. None of them may be read as a link."""
    targets = _targets("Reference/Tasks.md")
    assert targets == ["Chimera", "Projects/Meeting Notes"]


def test_a_link_inside_a_callout_is_still_a_link():
    """A callout is a blockquote, and its body is ordinary markdown — an edge
    written inside one is a real edge."""
    assert "Chimera" in _targets("Reference/Tasks.md")


# ══════════════════════════════════════════════════════════════════════════
# 9. The whole fixture, resolved end to end
# ══════════════════════════════════════════════════════════════════════════


def test_the_hubs_whole_link_set_resolves_as_expected():
    """One assertion over the entire specimen note, so a change that fixes one
    form by breaking another cannot pass. Written as the full expected list
    rather than as membership checks: a link that silently disappears is the
    failure mode, and only an exact list catches it."""
    resolved = [
        (link.kind, link.link_text, _resolve(link.target, "Hub.md"))
        for link in sorted(_links("Hub.md"), key=lambda link: link.position)
    ]
    assert resolved == [
        ("link", "[[Chimera]]", "Projects/Chimera.md"),
        ("link", "[[Chimera|the programme]]", "Projects/Chimera.md"),
        ("link", "[[Chimera#Goals]]", "Projects/Chimera.md"),
        ("link", "[[Chimera#^decision-1]]", "Projects/Chimera.md"),
        ("link", "[[Projects/Meeting Notes]]", "Projects/Meeting Notes.md"),
        ("link", "[[Meeting Notes]]", "Projects/Meeting Notes.md"),
        ("link", "[[Chimera Programme]]", "Projects/Chimera.md"),
        ("link", "[[prd-7]]", "Projects/Chimera.md"),
        ("link", "[[chimera]]", "Projects/Chimera.md"),
        ("markdown", "[the old plan](Archive/2019/Old Plan.md)",
         "Archive/2019/Old Plan.md"),
        ("markdown", "[the goals](Projects/Chimera.md#Goals)",
         "Projects/Chimera.md"),
        ("link", "[[Nowhere In Particular]]", None),
        ("embed", "![[Chimera]]", "Projects/Chimera.md"),
        ("embed", "![[Chimera#Goals]]", "Projects/Chimera.md"),
        ("embed", "![[Assets/diagram.png]]", None),
        ("link", "[[Assets/spec.pdf]]", None),
        ("link", "[[Board.canvas]]", None),
    ]


def test_every_fixture_note_survives_a_clean_for_embedding_round_trip():
    """A cleaner that raises on a real note shape takes the whole embed pass
    with it, and every version's frozen cleaner has to stay callable for as
    long as a row is stamped with it."""
    for rel in NOTE_PATHS:
        body = _body(rel)
        for version in (0, 1, 2, 3):
            assert isinstance(clean_at_version(version, body), str), (rel, version)
