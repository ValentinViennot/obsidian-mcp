# vault-history Specification

## Purpose
Answer "when, and by whom, was this written?" from the vault's own git
repository. The index knows what a note says now; only the repository knows
when a sentence first appeared and who put it there. Three read-only MCP tools
expose that, with every subprocess confined to `src/services/git_history.py`.

## Requirements

### Requirement: Note history with an explicit birth commit
`note_history(path, limit=50)` SHALL return the commits touching one note,
newest first, obtained with `git log --follow` so that renames are traced
through. Each commit SHALL carry its short and full sha, author name and
email, authored and committed timestamps as ISO 8601 **retaining the commit's
own UTC offset**, the subject, and how the path changed in that commit (added,
modified, renamed, copied, deleted). The response SHALL additionally report the
note's **birth commit** — the earliest commit in which the path appears — as a
distinct field resolved by its own query, so that a `limit` shorter than the
history does not cost the creation date. `limit` SHALL be clamped into
`1..MAX_HISTORY_COMMITS` rather than refused.

#### Scenario: A renamed note keeps its creation date
- **WHEN** a note was created under one path and renamed to another later
- **THEN** the history includes commits from before the rename, names the
  rename, and reports the creation date and original path of the first commit

#### Scenario: A limit shorter than the history
- **WHEN** `limit` admits fewer commits than the note has
- **THEN** the listed commits are the newest `limit`, and the birth commit is
  still reported

#### Scenario: An untracked note
- **WHEN** the path exists on disk but is in no commit
- **THEN** the response says the path is not tracked, and is not an error

### Requirement: Per-line attribution that survives reformatting
`note_blame(path, section=None, start_line=None, end_line=None)` SHALL return
per-line authorship from `git blame -w -M -C --line-porcelain`: line number,
line content, commit sha, author, and authored timestamp with its own offset.
`-w`, `-M` and `-C` are part of the contract, not a tuning: whitespace-only
changes, moves within a file and copies between files must not reassign
authorship. When a `.git-blame-ignore-revs` file exists at the vault root and
is a regular file, it SHALL be passed to git as `--ignore-revs-file`, and the
response SHALL state whether it was absent, applied, or refused by git. A
refusal SHALL degrade to a blame without it rather than failing the call.
`section` SHALL accept the heading selectors the read and write tools accept
and resolve to the line range that section occupies **in the file**, including
any frontmatter offset; it SHALL NOT be combinable with an explicit line range.
Attribution SHALL be taken from the **working tree**, so that line numbers
agree with what a read of the note returns, and a line present in the working
tree and in no commit SHALL be reported as uncommitted rather than attributed
to any commit.

#### Scenario: Uncommitted lines
- **WHEN** the note on disk has lines that are in no commit
- **THEN** the blame covers every line of the file on disk, and the
  uncommitted ones are marked as not committed rather than attributed

#### Scenario: A bulk reformat is looked through
- **WHEN** a commit listed in `.git-blame-ignore-revs` rewrote a line and the
  file is blamed
- **THEN** the line is attributed to the commit that wrote its content, not to
  the reformat, and the response says the ignore file was applied

#### Scenario: An unusable ignore file
- **WHEN** `.git-blame-ignore-revs` names something git cannot resolve
- **THEN** the blame is produced without it and the response says its
  revisions were not skipped

#### Scenario: Section-scoped blame
- **WHEN** `section` names a heading in a note carrying frontmatter
- **THEN** the blamed lines are that section's lines as git numbers them

### Requirement: Dating a string
`find_when_written(text, limit=20, path=None, regex=False)` SHALL run git's
pickaxe (`git log -S`), returning only commits that changed the **number of
occurrences** of `text`, in the commit shape `note_history` returns plus the
files each commit changed. `regex=True` SHALL add `--pickaxe-regex`. With
`path`, the search SHALL be scoped to that note and follow its renames; without
one it SHALL be scoped to the vault's own subtree. The registered docstring
SHALL state that this is the tool that answers "on {datetime} you wrote
{text}", because that docstring is what a connecting agent reads before
choosing a tool.

#### Scenario: The introducing commit, and only it
- **WHEN** one commit introduced a sentence and later commits edited the same
  file without changing that sentence
- **THEN** only the introducing commit is returned

#### Scenario: An argument that looks like an option
- **WHEN** `text` begins with `-` or contains shell metacharacters
- **THEN** it is matched literally and changes nothing about how git is invoked

### Requirement: Bounded, contained, off the event loop
Every git invocation SHALL use an explicit argv list and never a shell, SHALL
run under a wall-clock timeout and a stdout byte cap, and SHALL have its
process **group** killed when either bound is reached. Every `GIT_*` variable
SHALL be dropped from the child's environment and git's system and global
config layers SHALL be disabled. Every `path` argument SHALL be resolved
through the vault service's existing containment (`validate_visible_path`);
none of these tools SHALL implement containment of its own. All blocking git
work SHALL run off the event loop. Rendered responses SHALL respect
`MAX_READ_RESPONSE_CHARS`, and a blame SHALL additionally be bounded by
`MAX_BLAME_LINES` and `MAX_BLAME_LINE_CHARS`; every bound reached SHALL be
stated in the response rather than applied silently.

#### Scenario: A path that leaves the vault
- **WHEN** any of the three tools is given a traversing or dot-directory path
- **THEN** it is refused with the vault service's own wording and no git
  process is spawned for it

#### Scenario: git is absent or the vault is not a repository
- **WHEN** there is no `git` executable, or no repository above the vault root
- **THEN** the tool returns an in-band message naming what an operator would
  change, not an exception

#### Scenario: A pathological response
- **WHEN** a blame or a history would exceed the response cap
- **THEN** the response is cut and says it was cut, naming how to narrow it
