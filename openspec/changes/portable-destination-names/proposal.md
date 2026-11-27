# Make new destination names portable, and say so

## Why

The vault is synchronised by Git to Android (shared storage), Windows and macOS.
Git on Linux accepts names those platforms cannot hold: `?`, `:`, `"`, `*`, a
trailing dot or space, `CON.md`, and sibling folders that differ only by case
(`projects/KINKVERSE` next to `projects/kinkverse`). Agents writing through this
server have created such names and broken the phone's sync. Today the server
checks containment only, not portability.

The owner's decision: "Fix names automatically but not silently: the response
to the agent should explain what just happened to give them feedback".

## Scope

A pure module, `src/services/portable_path.py`, ports the vault checker's rules
(`scripts/check_portable_paths.py`). It applies **only to the destination** of
`create_note`, `move_note` (`to_path`), `write_file`, `request_upload` and
`import_from_url`, and only to path components that do not exist yet.
Existing names, reads, `edit_note` and a move's source are untouched.

- A changed name is written under its safe form; the response gains a
  `path_normalized: "<asked>" -> "<written>" (<reasons>)` line, and the vault
  commit message records it.
- A new folder that differs only by case or Unicode form from an existing
  sibling folder reuses the existing spelling, and says so.
- A new file name that collides with an existing sibling (by case, Unicode form,
  or because sanitising landed on an existing name) is refused with
  `path_case_collision` / `unportable_path`. Nothing is overwritten and no
  suffix is invented. A name with nothing left after sanitising is refused.
- The safe path replaces the caller's before `open_mutable`, so the existing
  no-clobber, `overwrite`, precondition and symlink checks all judge the path
  that will really be written.

## Impact

Tool descriptions of the five tools gain one sentence, so the published `tools/list` oracle (`tests/fixtures/tools_list_full_fork.json`) is regenerated deliberately (five description lines change; `move_note`'s description is tightened elsewhere to stay under the 4000-character cap). Two body refusal codes are added.
No database change, no new setting, no change for portable names (the input is
returned untouched, so existing responses are byte-identical).
