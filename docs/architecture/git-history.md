# Git history

> Read before touching `src/services/git_history.py`, the three history tools in `src/mcp_server/tools.py`, or the `GIT_HISTORY_*` / `MAX_BLAME_*` / `MAX_PICKAXE_*` constants in `src/config.py`.

The vault is a git repository with reconstructed history, and `note_history`,
`note_blame` and `find_when_written` answer the one class of question the index
cannot: *when*, and *by whom*, was this written. The headline use case is
"on {datetime} you wrote {text}" — `find_when_written`.

This is the **only subprocess surface in the codebase**. Everything below is
the reason each rule exists.

## The subprocess rules

- **Explicit argv, never `shell=True`, and no argv element built by
  concatenation.** The two caller-supplied values are a path and a pickaxe
  needle. The needle is the element immediately after `-S`, and the path is
  after a literal `--`, so neither can be read as an option however it begins —
  `find_when_written("--pickaxe-regex")` searches for that string. There is no
  shell, so shell metacharacters are characters.
- **The caller never names the repository.** `root` comes from
  `vault._vault_root`, and the relative path has already been through
  `validate_visible_path` — the same containment `read_note` has, including
  traversal refusal, the dot-directory guard and symlink resolution. Nothing in
  the git layer re-implements containment; a second, weaker copy of that logic
  is exactly how one of these ends up wrong.
- **Two bounds per invocation, and the process *group* is killed.** A
  wall-clock deadline (`GIT_HISTORY_TIMEOUT_SECONDS`) and a stdout byte cap
  (`GIT_HISTORY_MAX_OUTPUT_BYTES`). `subprocess.run` cannot express either: it
  reads to EOF, which is precisely the two failure modes worth preventing — a
  pickaxe over a huge history holding the thread for as long as git wants, and
  a `--line-porcelain` blame materialising an unbounded string. So stdout is
  read through `select` against a monotonic deadline. `start_new_session=True`
  plus `killpg` is what makes the kill total: git spawns helpers, and killing
  the leader alone leaves an orphan holding the pipe.
- **stderr goes to a temporary file, not a second pipe.** Reading two pipes in
  one loop is where this pattern deadlocks — git blocking on a full stderr
  buffer while the loop waits on stdout. A file cannot fill.
- **Every `GIT_*` variable is dropped from the child's environment.** `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` each
  silently point git at a repository other than the one this module resolved.
  An ambient one would make the tools answer, with no error at all, about a
  history that is not the vault's.
- **The config layers are pinned per call, not inherited.**
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, no pager, no terminal
  prompt, no opportunistic index refresh (`GIT_OPTIONAL_LOCKS=0` — these are
  read-only calls and the vault may be on a read-only mount), `LC_ALL=C`.
  Repository-local config (`.git/config`) is deliberately **kept**: it belongs
  to the vault and is already trusted by everything else that opens it.
- **`safe.directory` is passed explicitly, naming only the resolved
  directories.** Not theoretical: the vault is bind-mounted into the container,
  so the repository is routinely owned by a different uid than the server
  process, and git refuses such a repository outright ("dubious ownership").
  The global config that would normally carry the exemption is disabled above,
  so the exemption travels with the call. Never `safe.directory=*`.

## Why `-w -M -C` on blame is a contract, not a tuning

On a vault whose history is largely imports, reformats and reorganisations,
these three flags are most of the difference between useful attribution and
"everything was written by whoever last tidied it":

- `-w` — a re-indent or a trailing-whitespace strip does not reassign a line.
- `-M` — reordering sections within a note does not reassign its lines.
- `-C` — text split out of one note into another keeps its original author,
  and the response names the file it came from.

`.git-blame-ignore-revs` at the vault root covers what the flags cannot: a bulk
reformat that really did change content (bullet markers, frontmatter
normalisation, a `prettier` pass). It is passed as `--ignore-revs-file` only
when the path is a **regular file** — a symlink under a constant name is
somebody aiming the read somewhere the vault owner never named, so it is
refused rather than followed. When git rejects the file's contents the blame is
retried *without* it and the response says so: a broken ignore list is a reason
to give worse attribution, not none.

## Why the birth commit gets its own query

"When was this note created?" is the most-asked question of a vault, and the
newest `limit` commits do not contain the oldest one unless the note has fewer
than `limit` of them. So `note_history` runs a second, cheap
`--follow --diff-filter=A` log and takes its **oldest** record — oldest, because
a note deleted and re-created has more than one add and only the first is its
creation. If that query returns nothing, the tool falls back to the last record
of the follow log and marks the answer `birth_certain=False` rather than
presenting a date it cannot stand behind.

## Why both timestamps are always printed

On a reconstructed history the authored time is when the note was written
(backdated by the import) and the committed time is when the import ran. They
routinely differ by years. A tool that printed one and called it "the date"
would be wrong for whichever question the caller actually had. Both are
rendered as ISO 8601 **retaining the commit's own UTC offset** — "you wrote
this at 02:14" has to mean 02:14 where the author was, not in the server's
timezone. `git log`'s `%aI` renders UTC as `Z`; `_iso_from_epoch`, which
rebuilds blame's `<epoch> <+hhmm>` pair, matches that so the two tools speak one
dialect.

## Why the pickaxe and not a grep

`git log -S` selects commits where the **number of occurrences** of the string
changed. That is the whole tool: a commit that edits a file already containing
the text is not a match, so walking backwards, the last result is the commit
that wrote it. A `git log -G` or a grep over history would return every commit
that ever touched a file containing the phrase, which answers nothing.

`--follow` is added only when a single path scopes the search, because git
accepts it for exactly one pathspec. Unscoped, the search is limited to the
vault's own subtree (`-- .`, resolved against the child's `cwd`), so a vault
living inside a larger repository never answers about files outside it.

## The honest limitation

**Blame is only as good as the history underneath it.** On a vault where most
notes arrive in a single import commit, every line of those notes is authored
by the importer at the import's timestamp, and no flag changes that — `-w -M
-C` and `.git-blame-ignore-revs` improve attribution *across* commits, they
cannot manufacture commits that were never made. What a single-import history
does support is the note-level answer: `note_history`'s birth commit, backdated
per note from filesystem timestamps if the reconstruction did that, and
`find_when_written` dating a phrase to the import for text that arrived with
it and to a real commit for anything written since. Expect blame's value to
grow with the repository, not to be there on day one.

## Response bounds

Tool output is model input, and a blame or a history can run to far more text
than the note it describes. Every rendered response respects
`MAX_READ_RESPONSE_CHARS` like the read tools; a blame is additionally bounded
by `MAX_BLAME_LINES` (whole-note blame is capped, and `section=` /
`start_line`/`end_line` are how a caller reaches past it) and
`MAX_BLAME_LINE_CHARS` per line. **Every bound reached is stated in the
response.** A silently shortened history reads as a complete one, and for a
"when was this written" answer the record that gets dropped is the oldest —
i.e. the interesting one.
