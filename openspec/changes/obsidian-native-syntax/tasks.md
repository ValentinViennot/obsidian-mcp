The audit comes first and is a deliverable, not a preamble: the point of the
change is to know which gaps are real before closing any of them. Slices 2–5
are file-disjoint and could run in parallel; slice 6 depends on all of them.

## 1. Audit

- [x] 1.1 Read `src/services/links.py`, `extract_tags`, `clean_for_embedding`,
      the indexer's link pass, and the five graph tools; determine per feature
      whether it is handled correctly, partially, or not at all.
- [x] 1.2 Write `docs/architecture/obsidian-compatibility.md` as a table of
      feature → behaviour before → whether it matters → what was done, with an
      explicit "what was deliberately left" section. Out-of-scope is an
      acceptable verdict; silence is not.
- [x] 1.3 Index the note in `CLAUDE.md`'s architecture table and update the
      wikilink-graph key-decision bullet and the link-extraction paragraph.

## 2. Link resolution (owns `src/services/links.py`)

- [x] 2.1 `ExtractedLink` gains `anchor` and `display`, populated from the
      wikilink groups and from `MdLinkMatch.anchor`, plus `is_block_ref`.
      In-memory only — no `note_links` column, because `link_text` already
      carries both into the graph tools.
- [x] 2.2 The `%%…%%` grammar: matched pairs only, `comment_spans`,
      `mask_comments`, and `mask_code_and_comments` as the single ordered entry
      point. The comment above it states where comments are *not* masked and
      why, `mask_code` and section addressing first.
- [x] 2.3 `extract_links_bounded` masks through `mask_code_and_comments`.
- [x] 2.4 `build_vault_index` accepts 2- or 3-tuples and emits `paths`,
      `stems`, `paths_ci`, `stems_ci`, `aliases`. `normalize_aliases` reads the
      list, block-list and comma/newline-scalar forms, peels `[[…]]`, dedupes,
      and is bounded by `MAX_ALIAS_CHARS` / `MAX_ALIASES_PER_NOTE`.
- [x] 2.5 `_shortest_path_first`: same folder, fewest segments, shortest,
      alphabetical. Replaces the alphabetical tie-break; a single-candidate
      target is unaffected.
- [x] 2.6 `resolve_target` replays every path/stem rule case-folded, then
      consults aliases, with `follow_aliases` / `case_insensitive` flags
      defaulting to the correct behaviour. The `./`-normalisation is hoisted so
      the case-folded replay asks about the same path.

## 3. Tags (owns `src/services/vault.py`)

- [x] 3.1 `_INLINE_TAG_RE` — `\w` under Unicode plus `-` and `/`, leading
      boundary deliberately unchanged, with the reason the boundary is not
      widened written down.
- [x] 3.2 `_is_tag` — four exclusions, each documented as a rule about what a
      tag is, with the accepted false negatives named.
- [x] 3.3 `_mask_indented_code` — tag-extraction-only, list-context guarded,
      offset-preserving, with the false-negative it must not cause (a nested
      list item) stated.
- [x] 3.4 Frontmatter: the `tag:` singular key, `#`-prefix stripping,
      whitespace as well as comma splitting on scalars, one level of list
      flattening. The `_scrub_frontmatter` precondition is preserved — neither
      branch screens for itself.

## 4. Embedding text (owns `src/services/embeddings.py`)

- [x] 4.1 `clean_for_embedding` removes fenced spans then comment spans, in
      that order, so a `%%` inside a fence cannot pair with one outside it.
- [x] 4.2 `_v1_clean` freezes the versions-1-and-2 cleaner; registry entry 3
      binds the new one. The docstring says what deleting it would cost.

## 5. The index (owns `src/services/indexer.py`, `src/mcp_server/tools.py`)

- [x] 5.1 `CURRENT_EXTRACTION_VERSION = 3`, with the comment stating that this
      bump *does* move the embedding text but only for comment-carrying notes,
      and that no manual reindex is required.
- [x] 5.2 `_alias_aware_vault_index` — a `frontmatter -> 'aliases'` projection
      rather than the whole JSONB column, tolerant of a driver returning raw
      JSON text. Used by both the changed-path rebuild and the backfill.
- [x] 5.3 The re-resolution UPDATE folds in a newly-indexed note's
      **unambiguous** aliases, matched case-insensitively, under the same rule
      that governs the bare stem.
- [x] 5.4 `_MOVE_RESOLUTION` — `move_note` passes `follow_aliases=False,
      case_insensitive=False`, with the two reasons written at the constant.
- [x] 5.5 `_is_attachment_target` and `get_links`'s **Attachments** section,
      with the counter-example (a missing note with a dotted name stays
      dangling) in the comment.

## 6. Tests

- [x] 6.1 `tests/fixtures/obsidian_vault/` — a synthetic vault carrying every
      form: aliased links, heading and block anchors, note and attachment
      embeds, a basename collision across two folders, nested and Unicode tags,
      tags in fenced/inline/indented code, hex colours, a `%%comment%%`,
      callouts, tasks, footnotes, a canvas, and an Excalidraw note.
- [x] 6.2 `tests/test_obsidian_syntax.py` — extraction, resolution, tags,
      comments, and the deliberate non-behaviours (heading inside a comment
      still addressable; `move_note`'s narrow resolution; an unterminated `%%`
      hiding nothing). One end-to-end assertion over the hub note's whole link
      set, written as an exact list so a silently-vanishing link fails.
- [x] 6.3 A completeness guard: every `.md` under the fixture must be a path
      the module indexes, so a specimen added without registration is a
      failure rather than a silent gap.
- [x] 6.4 Update the version literal and the four row-fakes standing in for the
      vault-index SELECT, and add the version-3 scoped-re-embed assertions.
- [x] 6.5 `./scripts/test-in-docker.sh -q` green.
