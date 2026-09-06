## Why

This server's positioning is "agent memory stored as markdown you can open in
Obsidian". That claim is only true to the extent that the server reads
**Obsidian's** syntax rather than CommonMark's, and an audit of the extractors
found it reading plain markdown in three places that matter. None of them
failed loudly. Each produced an answer — a graph with edges missing, a tag
vocabulary made mostly of noise — which is the failure this project ranks
highest (`CLAUDE.md`: *silently wrong search results an agent acts on without a
human ever seeing the query*).

- **Frontmatter `aliases:` was not read at all.** A note declaring
  `aliases: [Chimera Programme, PRD-7]` had every link written through either
  name stored as a **dangling** row. `get_backlinks` on such a note was
  incomplete in the one way that cannot be noticed from the answer, and
  `find_orphans` reported it as an orphan. Aliases exist precisely on the notes
  with enough identity to be worth aliasing — the hubs.
- **An ambiguous basename resolved alphabetically.** Two notes sharing a
  basename is ordinary in a real vault, not pathological. `resolve_target` broke
  the tie with `sorted()`, so `Archive/2019/Meeting Notes.md` beat
  `Projects/Meeting Notes.md` for every source in the vault, because `A` sorts
  before `P`. Obsidian resolves the note nearest the source, then nearest the
  vault root. Nobody chose the alphabetical rule; it is what the list did.
- **Resolution was case-sensitive.** `[[project plan]]` is a working link in
  Obsidian and was a dangling row here.
- **The inline tag grammar was `#([a-zA-Z][a-zA-Z0-9_/-]*)`.** It matched
  `#ffe6cc`, so every colour in an unfenced diagram export was a tag — measured
  on a real vault, colour codes were the *majority* of the extracted
  vocabulary. It could not start with a digit (`#1password`), and it stopped at
  the first non-ASCII character, so `#projekt/größe` entered the vocabulary as
  `projekt/gr` and `#日本語` was not a tag at all: every non-English vault lost
  most of its tags to a character class. `get_tags` is one of the first calls an
  agent makes to learn a vault's conventions.
- **`%%comments%%` were not recognised anywhere.** A link inside one was a graph
  edge the author had explicitly withdrawn; a `#tag` inside one entered the
  vocabulary; and the whole comment was embedded. The largest single case is
  Excalidraw, whose plugin parks an entire scene — element ids, coordinates,
  colours — inside a `%%` block, so every drawing in a vault was contributing
  serialized geometry to vector space.

## What Changes

- **`resolve_target` resolves in Obsidian's order**: exact path → same-folder
  stem → vault-wide stem → the same three case-folded → frontmatter `aliases:`.
  Every new rule fires only where the old ones returned nothing, so no link
  that resolved before resolves anywhere else now. The one existing answer that
  moves is the ambiguity tie-break, which becomes same-folder, then fewest path
  segments, then shortest, then alphabetical for determinism.
- **`move_note` opts out of both widenings** (`_MOVE_RESOLUTION`). The rewriter
  decides what to overwrite on disk by asking `resolve_target`, so widening
  resolution widens what a move mutates; an alias travels in the moved note's
  own frontmatter and a stem is unchanged by a folder move, so neither widening
  earns a place on the destructive path. `move_note`'s on-disk behaviour is
  bit-identical to what it was.
- **`build_vault_index` carries aliases**, sourced from a `frontmatter ->
  'aliases'` projection rather than the whole JSONB column. The re-resolution
  pass attaches dangling rows on an unambiguous alias exactly as it does on an
  unambiguous stem.
- **The inline tag grammar becomes `\w` (Unicode) plus `-` and `/`**, with four
  exclusions stated as rules about what a tag is: no word character at all,
  purely numeric, a hex-colour shape carrying a digit, and a hex-colour shape
  that is one repeated character. Frontmatter gains the `tag:` singular key,
  `#`-prefix normalisation, whitespace splitting on scalars, and one level of
  list flattening. Tag extraction additionally masks **indented** code, which
  the shared fence grammar deliberately leaves alone, with a list-context guard
  so a nested list item is never mistaken for code.
- **`%%comments%%` are masked** for link extraction, tag extraction and
  `clean_for_embedding`, in that order after code masking. **Only matched
  pairs**: an unterminated `%%` hides nothing, because a flat scanner in which
  one stray marker deletes every edge below it is the failure the
  unterminated-fence rule already refuses. Comments are deliberately **not**
  masked for `mask_code` (and therefore section addressing), `move_note`, or
  `content_tsvector`.
- **`ExtractedLink` reports `anchor` and `display`**, in memory only —
  `note_links` gains no column, and `link_text` already carries both verbatim
  into the graph tools. `is_block_ref` distinguishes `#^block-id` from a
  heading anchor.
- **`get_links` classifies a non-markdown target as an attachment**, not a
  dangling link. `![[diagram.png]]` can never resolve because only `.md` is
  indexed; calling it broken invited agents to "fix" links that were never
  wrong.
- **`CURRENT_EXTRACTION_VERSION` 2 → 3**, with versions 1 and 2 frozen onto
  `_v1_clean`. The marker is the whole re-derivation mechanism, so **no manual
  reindex is required**; embedding invalidation stays scoped per note, so only
  notes containing a comment are re-embedded.
- **A synthetic fixture vault** (`tests/fixtures/obsidian_vault/`) and
  `tests/test_obsidian_syntax.py` assert every form above at once, including
  the deliberate non-behaviours.

No migration. No new dependencies. No new MCP tools — see the audit's "What was
deliberately left" for why.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `wikilink-graph`: resolution order gains case-folded filenames and
  frontmatter aliases; the ambiguity tie-break becomes Obsidian's
  shortest-path rule rather than alphabetical; comments are excluded from
  extraction; anchors and aliases are reported on the extracted link; a
  non-markdown target is reported as an attachment rather than dangling; the
  link-rewriting path resolves narrowly and unchanged.
- `search-quality`: the inline tag grammar excludes colour codes, issue
  numbers and preprocessor directives in indented code while admitting Unicode
  and leading-digit tags; frontmatter tag keys and forms are normalised; the
  embedding text excludes `%%comments%%`.
- `index-integrity`: the extraction marker moves to 3 and its re-derivation
  obligations are restated for a bump whose embedding text moves for a subset
  of notes.

## Impact

- `src/services/links.py` — the comment grammar, `mask_code_and_comments`,
  `ExtractedLink.anchor`/`display`, `build_vault_index`'s alias and case-folded
  maps, `normalize_aliases`, `_shortest_path_first`, `resolve_target`'s two
  flags.
- `src/services/vault.py` — `extract_tags`, `_is_tag`, `_INLINE_TAG_RE`,
  `_clean_frontmatter_tag`, `_mask_indented_code`.
- `src/services/embeddings.py` — `clean_for_embedding`, frozen `_v1_clean`,
  cleaner registry entry 3.
- `src/services/indexer.py` — `CURRENT_EXTRACTION_VERSION`,
  `_alias_aware_vault_index`, the alias arm of the re-resolution UPDATE.
- `src/mcp_server/tools.py` — `_MOVE_RESOLUTION`, `_is_attachment_target`,
  `get_links_impl`'s attachment section.
- `docs/architecture/obsidian-compatibility.md` (**new**, the audit),
  `CLAUDE.md`.
- `tests/fixtures/obsidian_vault/` (**new**), `tests/test_obsidian_syntax.py`
  (**new**), and the row-fake and version-literal updates in
  `tests/test_asvs_indexer_bounds.py`, `tests/test_issue_13_reresolve_shared_stem.py`,
  `tests/test_issue_91_indexed_root.py`, `tests/test_indexer_single_user_scope.py`.
