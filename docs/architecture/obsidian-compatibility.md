# Obsidian compatibility

**Read before changing `src/services/links.py`, `extract_tags`,
`clean_for_embedding`, or any graph tool.**

This server stores agent memory as markdown in an Obsidian vault. That is a
positioning claim, and it is only true to the extent that the server reads
Obsidian's syntax rather than CommonMark's. Where it read plain markdown, it
did not fail loudly — it produced a graph with edges missing and a tag
vocabulary made mostly of noise, and both are answers an agent acts on without
a human ever seeing the query. That is the failure this project ranks highest
(`CLAUDE.md`), arriving through the door marked "it's just markdown".

This note is the audit that preceded the fixes, kept as the record of what was
looked at, what was true, and what was deliberately left alone.

## The audit table

"Mattered" is judged against one question: **does getting this wrong give an
agent a confidently wrong answer?** A cosmetic gap that produces no answer at
all is a smaller problem than a silent one that produces the wrong edge.

| Feature | Behaviour before | Mattered? | What was done |
| --- | --- | --- | --- |
| `[[Wikilink]]` | Correct. Extracted, resolved filename-first with a same-folder bias. | — | Unchanged. |
| `[[Wikilink\|alias]]` | Target correct; the alias text was parsed and thrown away. | Low | The alias is now reported on `ExtractedLink.display`. Still not persisted — `link_text` already carries it verbatim into `get_links`. |
| `[[Wikilink#Heading]]` | Target correct; anchor parsed and thrown away. | Low | Reported as `ExtractedLink.anchor`. |
| `[[Wikilink#^block-id]]` | Target correct; anchor thrown away, and nothing could tell a block reference from a heading. | Low | `anchor` keeps the `^`, and `ExtractedLink.is_block_ref` says which it is. |
| `![[Embed]]` (note) | Correct — extracted with `kind="embed"` and resolved. | — | Unchanged. A transclusion is the strongest form of "this note depends on that one" and it was already an edge. |
| `![[image.png]]`, `[[report.pdf]]` | Stored as a **dangling** link, i.e. reported to agents as a broken reference. | **Yes** | Still unresolvable — only `.md` is indexed — but `get_links` now reports these under **Attachments** with a pointer to `read_file`, instead of inviting an agent to "fix" links that were never wrong. On an attachment-heavy note this was most of the dangling list. |
| `[[folder/Note]]` vs bare | Correct, including `./` and `../` in markdown hrefs. | — | Unchanged. |
| **Basename ambiguity** | Same-folder first, then **alphabetically first**. | **Yes** | Now Obsidian's rule: same folder, then fewest path segments (nearest the vault root), then shortest, then alphabetical for determinism. The old rule handed every ambiguous `[[Meeting Notes]]` to `Archive/2019/Meeting Notes.md` because `A` sorts before `P` — a note nobody meant, quietly, for every source in the vault. |
| **Case** | Case-**sensitive**. `[[project plan]]` did not find `Project Plan.md`. | **Yes** | Case-insensitive resolution, replayed only after every case-exact rule fails, so it can turn a dangling link into a resolved one and can never re-point one that already resolved. |
| **Frontmatter `aliases:`** | **Not read at all.** Every link written through an alias was stored dangling. | **Yes — the largest gap** | Resolution consults aliases last, after every filename rule. List, block-list and comma-scalar forms all read; `[[…]]`-wrapped aliases peeled; case-folded. The re-resolution pass also attaches dangling rows when a note arrives declaring an unambiguous alias. |
| Frontmatter `cssclasses`, arbitrary properties | Carried verbatim in `notes_metadata.frontmatter`, filterable, not interpreted. | — | Unchanged, deliberately. |
| **Inline `#tag`** | `#([a-zA-Z][a-zA-Z0-9_/-]*)`. | **Yes** | Rewritten — see "Tags" below. |
| Nested `#parent/child` | Correct. | — | Unchanged. |
| Frontmatter `tags:` | List and comma-scalar forms read. `tag:` (singular) ignored; a leading `#` not stripped; a space-separated scalar read as one tag. | Medium | `tag:` read; `#work` and `work` normalised to one tag; scalars split on whitespace as well as commas; nested lists flattened one level. |
| `#` in fenced / inline code | Already masked (#14). | — | Unchanged. |
| `#` in **indented** code | **Not masked** — the fence grammar deliberately skips 4-space blocks. `#include`, `#define`, `#region` were tags. | Medium | A tag-extraction-only indented-code masker, with a list-context guard so a nested list item is never mistaken for code. |
| Hex colours (`#ffe6cc`) | **Tags.** | **Yes** | Excluded — see "Tags". |
| URL fragments | Already excluded by the leading-boundary rule. | — | Unchanged. |
| Callouts `> [!note]` | A blockquote; links inside resolve, the marker is not a link. Correct by accident and now by test. | — | Pinned in the fixture. |
| Tasks `- [ ]` / `- [x]` | Not read as links (correct); not otherwise addressable. | Low | Pinned in the fixture. **No task tool added** — see "What was deliberately left". |
| Footnotes `[^1]`, `[^1]:` | Not read as links. Correct. | — | Pinned in the fixture. |
| Attachments as link targets | See `![[image.png]]` above. | — | — |
| **`%%comments%%`** | **Not recognised anywhere.** Links inside one were graph edges, tags inside one entered the vocabulary, and the text was embedded. | **Yes** | Masked for link extraction, tag extraction and the embedding text. Not for `mask_code`, section addressing, `move_note` or `content_tsvector` — see "Where comments are not masked". |
| `.canvas` | Not indexed (the scan takes `.md` only); readable and writable as bytes through `read_file` / `write_file`. | — | Unchanged, and that is the right answer: nothing parses or rewrites a canvas, so nothing can corrupt one. `.canvas` is in the attachment-suffix set so `[[Board.canvas]]` reads as an attachment rather than a broken link. A fixture canvas is asserted to still parse as JSON. |
| Excalidraw (`*.excalidraw.md`) | Indexed as markdown. The scene JSON sat in a `%%` block, so **every drawing in the vault contributed serialized geometry to vector space.** | **Yes** | Falls out of the comment fix. `find_related` on a drawing no longer returns other drawings for being drawings. |

## The three fixes that mattered

### 1. Link resolution

`resolve_target` gained three rules, in Obsidian's own order, each strictly
after the rules that came before it:

```
exact path → same-folder stem → vault-wide stem → the same three, case-folded → aliases
```

Additivity is the safety argument. Every new rule fires only where the old ones
returned nothing, so no link that resolved before resolves anywhere else now.
The one behaviour that *changes* an existing answer is the ambiguity
tie-break, and it changes it from "alphabetically first" — which is not a rule
anybody chose, just what `sorted()` did — to Obsidian's.

**`move_note` opts out of both widenings**, via `_MOVE_RESOLUTION` in
`src/mcp_server/tools.py`. The rewriter decides which links on disk to
overwrite by asking `resolve_target` what they point at, so widening
resolution widens what a move mutates. Neither widening earns that, and for the
same reason in both cases — the answer does not move with the file:

* an alias lives in the moved note's own frontmatter and travels with it, so
  `[[Alias]]` still resolves after the move; rewriting it to `[[New]]` would
  destroy an alias the author chose, to fix nothing;
* a case-differing bare name resolves by stem, and a folder move does not
  change the stem.

`move_note`'s on-disk behaviour is therefore bit-identical to what it was.
**Residual:** a move that also *renames* the note leaves a case-differing bare
link (`[[old plan]]` → `Old Plan.md` → `New Plan.md`) dangling, where Obsidian
would have re-pointed it. Accepted: closing it means widening the destructive
path, and it needs its own adversarial pass.

### 2. Tags

The old grammar was `#([a-zA-Z][a-zA-Z0-9_/-]*)`, and three of its four
properties were wrong:

* **`#ffe6cc` starts with a letter.** Every fill and stroke in an unfenced
  diagram export, inline `<span style="…">` or mermaid snippet was a tag.
  Measured on a real vault, colour codes were the *majority* of the extracted
  vocabulary — a taxonomy whose most common member is `#d5e8d4` is not a
  taxonomy, and `get_tags` is one of the first calls an agent makes to learn a
  vault's conventions.
* **It could not start with a digit** — `#1password`, `#3d-printing` were not
  tags.
* **It stopped at the first non-ASCII character.** `#projekt/größe` entered the
  vocabulary as `projekt/gr`; `#日本語` was not a tag at all. Every vault not
  written in English lost most of its tags to a character class.

The new grammar is `\w` under Unicode plus `-` and `/`, with four exclusions,
each a statement about what a tag *is* rather than a blocklist of words
(`_is_tag` in `src/services/vault.py` carries the reasoning per rule):

1. no word character at all (`#-`, `#--`);
2. purely numeric (`#42`, `#2024`) — Obsidian's own rule, and what keeps every
   issue reference out;
3. a hex-colour shape (3, 6 or 8 hex characters) **that contains a digit** —
   the digit is what separates a colour from a word, so `#facade` and `#decade`
   survive;
4. a hex-colour shape that is one repeated character (`#fff`, `#ccc`,
   `#eeeeee`) — the greys and whites, which carry no digit.

**The accepted false negatives**, all deliberate: `#facade`-shaped words are
kept as tags even when they were colours; `(#tag)` is not recognised, because
widening the leading boundary to `(` and `[` would read `[Jump](#heading)`,
`[[#Heading]]` and `href="#frag"` as tags, and a false tag is the error being
removed here; and `#include` written in *prose* is still a tag.

### 3. Comments

`%%…%%` hides text in Obsidian, so commented text is not part of what a note
says. Three consumers now agree with that: link extraction, tag extraction and
`clean_for_embedding`.

**The grammar is deliberately narrower than Obsidian's: only matched pairs.**
An unterminated `%%` comments out the rest of the note in Obsidian; here it
hides nothing. A flat scanner in which one stray marker silently deletes every
edge below it is the same "one line eats the rest of the file" failure the
unterminated-fence rule already refuses — except that this one would delete
graph edges rather than refuse a write.

**Order is part of the grammar**: code first, comments second
(`mask_code_and_comments`). A `%%` inside a fence is already spaces by then and
cannot open a comment, so a shell script cannot hide half a note.

#### Where comments are **not** masked, and why

* **`mask_code` itself, and therefore `_scan_headings`.** A heading inside a
  comment must stay addressable. Section reads and `edit_note(section=…)`
  resolve over the same scan, and making a heading invisible to the read side
  while the write side still counts it is exactly the destructive round-trip
  class #140 closed. `tests/test_obsidian_syntax.py` pins the Excalidraw
  note's `# Drawing` heading — which lives inside a `%%` block — as still
  visible to `_scan_headings`.
* **`move_note`'s rewriter.** Rewriting a commented link keeps it pointing at
  the note the author meant; leaving it stale would not.
* **`content_tsvector`.** Keyword search answers "which file contains these
  bytes", and the bytes are in the file. Narrowing it would also cost a
  `make rebuild-tsvectors` for recall this server does not want back.

## Deployment: is a reindex needed?

**No manual step.** `CURRENT_EXTRACTION_VERSION` moves 2 → 3, which is exactly
the mechanism for a change `content_hash` cannot see (the bytes on disk do not
move). The next index pass reads every row's marker as stale and therefore
treats every note as changed: re-parsed, re-tagged, re-linked, keyword vector
rewritten, marker re-stamped. `make reindex` and `make rebuild-tsvectors` are
**not** required.

Embeddings are the one place with a cost, and it is scoped rather than
blanket. `_grammar_changed_the_embedding_text` compares what the row's
*stamped* version would have embedded against what version 3 embeds, per note,
so `embedded_content_hash` is cleared for **exactly the notes that contain a
`%%comment%%`** — in most vaults, the Excalidraw notes and little else. Every
other note keeps its vectors. `_v1_clean` in `src/services/embeddings.py` is
the frozen versions-1-and-2 cleaner and must not be deleted or re-pointed while
any row is stamped with either: doing so would invalidate every vector in the
vault.

The first pass after deploy is therefore a full link-and-tag re-derivation
(cheap, no provider calls) plus a re-embed of the comment-carrying notes.

## What was deliberately left

* **No new MCP tools.** Every gap found was a correctness gap in tools that
  already exist, and the fixes propagate: `get_backlinks`, `get_neighborhood`
  and `find_orphans` all read the same resolved-edge table, so aliases
  resolving means fewer missing backlinks, denser neighbourhoods and fewer
  false orphans, with no new surface. A tool costs context in *every*
  connecting agent on *every* session; "resolve an alias to a note" is
  `keyword_search` with extra steps, and a task-listing tool is a product
  decision about what this server is for, not a compatibility fix. Neither
  earns its 29th slot today.
* **Anchors are not persisted.** Storing `anchor` and `display` in
  `note_links` needs a migration, and it buys little: `link_text` already
  carries `[[Note#Heading|alias]]` verbatim into `get_links` and
  `get_backlinks`, so an agent can already see what a link addressed. The
  parsed fields exist in memory for a future consumer that needs them without
  re-splitting a string.
* **Block ids are not an addressing scheme.** `^block-id` is recognised as a
  link *anchor*; there is no `read_note(block=…)`. Section addressing already
  covers the case agents actually use, and a second addressing scheme over the
  same notes is a second thing to keep consistent with the write path.
* **Canvas contents are not indexed.** A `.canvas` names notes in its JSON, so
  in principle it carries graph edges. Parsing it would mean a second link
  grammar over a format that is not markdown, and writing it back is a
  corruption risk on a file type Obsidian owns. Byte transport is the right
  contract.
* **Dataview / Templater / Tasks plugin syntax** is out of scope. It is
  plugin-defined, changes between plugin versions, and is not part of what
  Obsidian itself renders.
* **Frontmatter is not keyword-searchable.** `content_tsvector` is built from
  the post-frontmatter body, so a note's aliases and properties do not answer
  `keyword_search`. Changing that means a `make rebuild-tsvectors` on every
  deployment and is a separate change with its own recall trade-off.

## The fixture vault

`tests/fixtures/obsidian_vault/` is a small synthetic vault written for
`tests/test_obsidian_syntax.py`: every note in it is a deliberate specimen of
one piece of syntax, and nothing in it came from anybody's real vault. It is
the durable part of this work. Each failure above is invisible from inside the
server — the tool answers, the answer is just missing edges or full of noise —
so a fixture that pins the whole grammar at once is what stops them coming back
one at a time. Add a specimen when adding a rule; the module asserts that every
`.md` under the fixture is one it indexes, so a note added without being
registered is a test failure rather than a silent gap.
