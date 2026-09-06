---
cssclasses:
  - wide-table
---

# The synthetic Obsidian fixture vault

Every note under this directory is **invented for the test suite**. Nothing
here came from anybody's real vault, and nothing here should ever be edited to
match one: the point is that each file is a deliberate specimen of one piece of
Obsidian syntax, so a change to the extractors either keeps the assertions in
`tests/test_obsidian_syntax.py` true or is a declared behaviour change.

The specimens, and what each exists to pin:

| File | Pins |
| --- | --- |
| `Hub.md` | every link form at once — bare, aliased, anchored, block-referenced, embedded, path-style, attachment, dangling, markdown, and links hidden in code and in a `%%comment%%` |
| `Projects/Chimera.md` | frontmatter `aliases:`, a block id, arbitrary properties |
| `Projects/Meeting Notes.md`, `Archive/2019/Meeting Notes.md` | the basename collision, and which of the two an ambiguous `[[Meeting Notes]]` picks from three different source folders |
| `Reference/Colours.md` | the things that look like tags and are not: hex colours, issue numbers, preprocessor directives in indented code, URL fragments |
| `Reference/Tasks.md` | callouts, task lists, footnotes, nested and Unicode tags |
| `Drawings/Sketch.excalidraw.md` | the `%%`-wrapped scene an Excalidraw note carries, and the heading inside it that must stay addressable |
| `Board.canvas` | a canvas file, which is JSON and is not a note |
| `Assets/*` | link targets that are attachments rather than broken links |

`README.md` itself is a specimen too: `cssclasses` is an Obsidian property this
server carries in `frontmatter` and deliberately does not interpret.
