---
tag: single-key-form
---

# Callouts, tasks and footnotes

> [!note] A callout
> Callouts are blockquotes with a type marker. The marker is not a link, and
> the body is ordinary markdown: [[Chimera]] resolves from inside one.

> [!warning]- A collapsed callout
> The trailing `-` folds it. Still not a link.

## Tasks

- [ ] An open task with a tag #todo
- [x] A completed task
- [ ] A task that links to [[Projects/Meeting Notes]]
    - [ ] A nested subtask #todo/soon

## Footnotes

A claim that needs a source.[^src] And another.[^2]

[^src]: The source. Not a markdown link, not a wikilink.
[^2]: Nor is this.
