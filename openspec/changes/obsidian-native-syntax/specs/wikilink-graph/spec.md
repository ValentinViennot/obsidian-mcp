## MODIFIED Requirements

### Requirement: Wikilink target resolution

The system SHALL resolve each extracted link's target string to a
`target_note_id` when an existing note matches; otherwise the row SHALL be
stored with `target_note_id = NULL` (dangling).

Resolution SHALL follow Obsidian's order, and each rule SHALL be attempted only
when every rule before it has produced no match:

1. path-style — a target containing `/`, or carrying a `.md` extension, matched
   against the stored path with `./` and `../` resolved against the source's
   folder;
2. same-folder — `<source-dir>/<target>.md`;
3. vault-wide basename;
4. rules 1–3 again, **case-folded**;
5. frontmatter `aliases:`, case-folded.

The ordering is a requirement, not an implementation detail. Rules 4 and 5 fire
only where the case-exact filename rules found nothing, so they can convert a
dangling link into a resolved one and SHALL NOT be able to re-point a link that
already resolved. Rule 5 comes last so that a note actually named `X` always
wins over a note that merely declares `X` as an alias.

#### Scenario: Path-style wikilink resolves to exact path

- **WHEN** a wikilink is `[[Folder/Subfolder/Note]]` and a note exists at `Folder/Subfolder/Note.md`
- **THEN** the link's `target_note_id` SHALL be set to that note's ID

#### Scenario: Bare-name wikilink prefers same-folder match

- **WHEN** the link is `[[Foo]]` and notes exist at both `<source-dir>/Foo.md` and `Other/Foo.md`
- **THEN** resolution SHALL prefer `<source-dir>/Foo.md`

#### Scenario: Bare-name wikilink with single match across vault

- **WHEN** the link is `[[Foo]]`, no same-folder match exists, and exactly one note in the vault has stem `Foo`
- **THEN** that note SHALL be selected

#### Scenario: Ambiguous bare name resolves to the nearest note

- **WHEN** the link is `[[Foo]]`, no same-folder match exists, and several notes share the stem `Foo`
- **THEN** the note with the fewest path segments SHALL be selected — the one nearest the vault root
- **AND** ties among those SHALL be broken by shortest path and then alphabetically, so the result is deterministic
- **AND** the original target string SHALL still be stored in `target_path`

#### Scenario: The tie-break is not alphabetical

- **WHEN** the link is `[[Meeting Notes]]` from a source in neither folder, and the vault holds both `Archive/2019/Meeting Notes.md` and `Projects/Meeting Notes.md`
- **THEN** `Projects/Meeting Notes.md` SHALL be selected
- **AND** the fact that `Archive/…` sorts alphabetically first SHALL have no bearing on the result

#### Scenario: A link resolves through a frontmatter alias

- **WHEN** a note declares `aliases: [Chimera Programme, PRD-7]` in its frontmatter and another note links `[[Chimera Programme]]`
- **THEN** the link SHALL resolve to the aliasing note
- **AND** the alias forms `aliases: [A, B]`, a YAML block list, and the scalar `aliases: A, B` SHALL all be read

#### Scenario: A filename beats an alias

- **WHEN** a note is named `Real.md` and a different note declares `Real` as an alias
- **THEN** `[[Real]]` SHALL resolve to `Real.md`

#### Scenario: Filename resolution ignores case

- **WHEN** the link is `[[project plan]]` and the vault holds `Project Plan.md`
- **THEN** the link SHALL resolve to that note

#### Scenario: Unresolved link stored as dangling

- **WHEN** no note matches the resolution rules
- **THEN** the row SHALL be stored with `target_note_id = NULL` and `target_path` equal to the original target string (without alias or anchor)

#### Scenario: Anchor and alias do not change resolution

- **WHEN** the link is `[[Foo#Heading|alias]]`
- **THEN** resolution SHALL operate on `Foo` only
- **AND** the full original text SHALL be preserved in `link_text`

#### Scenario: Re-resolution when a target is created

- **WHEN** a note is created or its path changes
- **THEN** the indexer SHALL update any rows in `note_links` whose `target_path` would now resolve to that note, setting their `target_note_id` accordingly
- **AND** an **unambiguous** frontmatter alias of that note SHALL be matched in the same statement, case-insensitively, under the same rule that governs the bare stem — an alias claimed by two notes SHALL leave the rows dangling rather than guess

#### Scenario: Re-resolution when a target is deleted

- **WHEN** a note is deleted
- **THEN** rows in `note_links` whose `target_note_id` referenced the deleted note SHALL have `target_note_id` set back to NULL

### Requirement: Link extraction during indexing

The system SHALL extract `[[wikilinks]]`, `![[embeds]]`, and `[label](path.md)`
markdown links from every note's body during indexing and persist them to a
`note_links` table. Extraction SHALL be bounded per note by
`MAX_LINKS_PER_NOTE` (10,000), applied in document order; a note over the cap
is a declared degradation (see "Link extraction is linear-time and bounded per
note"), not a skip.

Extraction SHALL ignore text that Obsidian does not render: fenced code, inline
code, **and matched `%%comment%%` spans**. A commented-out link is a
relationship the author explicitly withdrew, and counting it as an edge makes
`get_backlinks` assert a claim the note does not make.

The extracted link SHALL additionally report the wikilink's `#anchor` and
`|alias` parts as parsed fields, distinguishing a block reference (`#^id`) from
a heading anchor, so that no consumer has to re-split `link_text` with a second
grammar. These fields are not persisted; `link_text` already carries them
verbatim.

#### Scenario: Wikilinks captured

- **WHEN** the indexer processes a note containing `[[Project Plan]]` and `[[Folder/Other Note|alias]]`
- **THEN** two rows SHALL be inserted into `note_links` with `source_note_id` equal to the indexed note's ID, `link_text` set to the original wikilink text including any alias/anchor, and `kind` set to `"link"`

#### Scenario: Embeds captured separately

- **WHEN** the indexer processes a note containing `![[Diagram.md]]`
- **THEN** a row SHALL be inserted with `kind = "embed"`

#### Scenario: Markdown links to .md files captured

- **WHEN** the indexer processes a note containing `[See also](./Subfolder/Note.md)`
- **THEN** a row SHALL be inserted with `kind = "markdown"` and `target_path` set to the resolved relative path

#### Scenario: Code blocks ignored

- **WHEN** the indexer processes a note where `[[Foo]]` appears inside a fenced code block (` ``` `) or inline code (`` ` ``)
- **THEN** no row SHALL be inserted for that occurrence

#### Scenario: Comments ignored

- **WHEN** the indexer processes a note where `[[Foo]]` appears inside a matched `%%…%%` span
- **THEN** no row SHALL be inserted for that occurrence

#### Scenario: An unterminated comment marker hides nothing

- **WHEN** a note contains a `%%` with no closing `%%` below it
- **THEN** every link after that marker SHALL still be extracted
- **AND** the system SHALL NOT treat the remainder of the note as commented

#### Scenario: A comment marker inside code cannot open a comment

- **WHEN** a `%%` appears inside a fenced code block and another `%%` appears in the prose below it
- **THEN** the text between them SHALL NOT be treated as a comment

#### Scenario: Re-extraction on content change

- **WHEN** a note's `content_hash` changes between index runs
- **THEN** existing rows in `note_links` for that `source_note_id` SHALL be deleted and replaced with the freshly-extracted set — the first `MAX_LINKS_PER_NOTE` links in document order — in the same database transaction as the metadata upsert

### Requirement: `get_links` MCP tool

The system SHALL expose an MCP tool `get_links(path)` that returns the list of
links emanating FROM the note at `path`, distinguishing resolved links,
dangling links, and **attachments**.

An unresolved link whose target names a non-markdown file — an image, a PDF, a
media file, a `.canvas` — SHALL be reported as an attachment and not as a
dangling link. Only `.md` is indexed, so such a link can never resolve however
healthy the vault is, and reporting it as broken invites an agent to repair
links that were never wrong. An unresolved target that does *not* name a known
attachment type SHALL still be reported as dangling, including one whose name
merely contains a dot.

#### Scenario: Resolved and dangling shown together

- **WHEN** the source note has both resolved links and dangling references
- **THEN** the response SHALL include both, each row carrying `target_path`, `target_title` (NULL for dangling), `kind` (`link`/`embed`/`markdown`), `link_text`, `resolved` (boolean), and `position`

#### Scenario: An attachment embed is not reported as broken

- **WHEN** the source note contains `![[Assets/diagram.png]]`
- **THEN** the response SHALL list it under attachments, naming the tool that can read it
- **AND** it SHALL NOT be counted among the note's dangling links

#### Scenario: Source note has no outgoing links

- **WHEN** the source note contains no link of any kind
- **THEN** the system SHALL return an empty result set with an explanatory message

## ADDED Requirements

### Requirement: Link rewriting resolves more narrowly than the graph

`move_note`'s link rewriting SHALL resolve link targets **without** frontmatter
aliases and **without** case-folded filename matching, even though the indexer
resolves with both.

The rewriter decides which links on disk to overwrite by asking what they
resolve to, so every widening of resolution is a widening of what a move
mutates. Neither widening earns a place on that path, and for the same reason
in both cases — the answer does not move with the file. An alias lives in the
moved note's own frontmatter and travels with it, so `[[Alias]]` still resolves
after the move and rewriting it would destroy an alias the author chose. A
case-differing bare name resolves by stem, and a folder move does not change
the stem.

The residual SHALL be documented rather than closed: a move that also *renames*
a note leaves a case-differing bare link dangling until it is re-pointed.

#### Scenario: A move does not rewrite an aliased link

- **WHEN** `move_note` moves a note that another note references by one of its frontmatter aliases
- **THEN** the aliased link SHALL be left exactly as written
- **AND** it SHALL still resolve to the moved note at the next index pass

#### Scenario: A move does not rewrite a case-differing link

- **WHEN** `move_note` moves `Old Plan.md` to another folder and another note links `[[old plan]]`
- **THEN** that link SHALL be left exactly as written
