## ADDED Requirements

### Requirement: The tag vocabulary is a taxonomy, not a token dump

`get_tags`, the `tags=` filters and `notes_metadata.tags` are how an agent
learns a vault's conventions on its first call, so a vocabulary dominated by
noise is worse than a small one. Inline tag extraction SHALL recognise
Obsidian's tag grammar — a `#` at a whitespace or line-start boundary followed
by letters, digits, `_`, `-`, `/`, in **any script** — and SHALL exclude four
shapes that merely start with `#`:

1. a token with no word character at all;
2. a purely numeric token (Obsidian's own rule, and what keeps issue
   references out);
3. a hex-colour shape (3, 6 or 8 hex characters) that contains a digit;
4. a hex-colour shape that is a single repeated character.

Rules 3 and 4 exist because colour codes were, measured on a real vault, the
**majority** of the extracted vocabulary: an unfenced diagram export, an inline
`style` attribute or a mermaid snippet contributed one tag per fill and stroke.
The digit test is what separates a colour from a word, so an all-alphabetic
hex-shaped word (`#facade`) SHALL be kept — losing a real tag is the worse
error.

Extraction SHALL additionally ignore `#` tokens inside **indented** code
blocks, which the shared fence recognizer deliberately does not mask. It SHALL
NOT mistake a nested list item for indented code: a list item four spaces deep
is the commonest construct in an Obsidian note and its tags are real.

Frontmatter tags SHALL be read from both `tags:` and the singular `tag:`; a
leading `#` SHALL be stripped so `#work` and `work` are one tag and not two; a
scalar value SHALL split on whitespace as well as commas; and a list nested one
level SHALL be flattened.

#### Scenario: Colour codes are not tags

- **WHEN** a note contains `#ffe6cc`, `#d5e8d4`, `#fff` or `#eeeeee` outside a code fence
- **THEN** none of them SHALL appear in the note's tags

#### Scenario: Issue references are not tags

- **WHEN** a note contains `#42` or `#1234`
- **THEN** neither SHALL appear in the note's tags

#### Scenario: Unicode and leading-digit tags are tags

- **WHEN** a note contains `#projekt/größe`, `#日本語`, `#3d-printing` or `#1password`
- **THEN** all four SHALL appear in the note's tags, complete and untruncated

#### Scenario: Preprocessor directives in indented code are not tags

- **WHEN** a note contains a four-space-indented block holding `#include`, `#define` and `#region`
- **THEN** none of them SHALL appear in the note's tags

#### Scenario: A nested list item keeps its tags

- **WHEN** a note contains a list item indented four spaces carrying `#reference/nested`
- **THEN** that tag SHALL appear in the note's tags

#### Scenario: Frontmatter tag forms are normalised

- **WHEN** a note's frontmatter carries `tags: ["#design"]`, or `tag: single`, or `tags: work personal`
- **THEN** the tags SHALL be `design`, `single`, and `work` plus `personal` respectively

### Requirement: Commented text is not embedded

`clean_for_embedding` SHALL remove matched `%%…%%` spans as well as fenced code
blocks, comments after fences so that a `%%` inside a fence cannot pair with
one outside it.

Commented text is not rendered, so it is not part of what a note says, and the
vector that decides what `semantic_search` and `find_related` return was being
built partly from it. The dominant case is Excalidraw, whose plugin stores an
entire scene — element ids, coordinates, colours — inside a `%%` block, so
every drawing in a vault contributed serialized geometry to vector space and
`find_related` on a drawing could return other drawings for being drawings.

The keyword vector (`content_tsvector`) SHALL NOT be narrowed in the same way:
keyword search answers "which file contains these bytes", and the bytes are in
the file.

#### Scenario: A comment does not reach the embedding text

- **WHEN** a note contains a `%%…%%` span
- **THEN** the text embedded for that note SHALL exclude the span
- **AND** the surrounding prose SHALL be retained

#### Scenario: An Excalidraw scene does not reach the embedding text

- **WHEN** a note carries an Excalidraw scene inside a `%%` block
- **THEN** the embedded text SHALL contain the note's text elements and none of the scene JSON

#### Scenario: Keyword search still finds a comment

- **WHEN** a term appears only inside a `%%…%%` span
- **THEN** `keyword_search` SHALL still be able to return the note
