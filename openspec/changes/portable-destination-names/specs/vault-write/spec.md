## ADDED Requirements

### Requirement: New destination names are made portable and the response says how

The system SHALL make a **new** path component portable before `create_note`, `move_note` (its `to_path` only), `write_file`, `request_upload` or `import_from_url` publishes or mints for it, because the vault is synchronised to Android and Windows. A component that already exists SHALL never be changed, whatever it contains. Reads, `edit_note` and the source of a move SHALL be unaffected.

For a new component the system SHALL: normalise it to Unicode NFC; replace each of `< > : " \ | ? * [ ] # ^` and the control characters U+0000–U+001F and U+007F with `-` (dropping a trailing `?` or `"`), collapsing the runs this produces; strip trailing spaces and dots; append `-` to the stem of a Windows device name (`CON PRN AUX NUL COM0-9 LPT0-9`); and shorten a component over 240 UTF-8 bytes at a character boundary, keeping its extension. Dot-prefixed names, `.` and `..` SHALL be left to the existing path guard.

The corrected path SHALL replace the caller's path **before** the tool's own validation, so that the existing exists, no-clobber, `overwrite`, precondition and symlink checks run on the path that will be written. Normalisation SHALL NOT cause an existing file to be overwritten.

When a name changed, the success response SHALL report the path actually used and SHALL carry one line, `path_normalized: "<requested>" -> "<used>" (<reasons>)`, and the vault commit message SHALL record it. When no name changed the response SHALL be unchanged.

#### Scenario: A forbidden character is replaced and reported

- **WHEN** `create_note` is called with `Ideas/Plan: phase 1.md` and the note does not exist
- **THEN** the note SHALL be written at `Ideas/Plan - phase 1.md`
- **AND** the response SHALL contain a `path_normalized:` line naming both paths and the reason

#### Scenario: A trailing question mark is dropped

- **WHEN** `write_file` is called with `Why now?.md`
- **THEN** the file SHALL be written as `Why now.md` and the response SHALL say so

#### Scenario: An existing historical name is never changed

- **WHEN** a path names components that already exist, however unportable
- **THEN** those components SHALL be used exactly as they are
- **AND** only a new component beneath them SHALL be normalised

#### Scenario: Sanitising never overwrites

- **WHEN** the normalised file name equals an existing file
- **THEN** the tool SHALL refuse with `unportable_path`, name the existing path, and write nothing, including under `overwrite=True`

### Requirement: A new name that differs from an existing sibling only by case or Unicode form

For a new component whose NFC, case-folded form equals that of an existing sibling, the system SHALL reuse the existing **folder's** spelling and report it with a `path_normalized:` line. A new **file** name that collides with an existing sibling this way SHALL be refused with `path_case_collision`, naming the existing path, and SHALL write nothing and invent no suffix. A name with nothing left after normalisation SHALL be refused with `unportable_path`. A case-only rename of a move's own source SHALL remain possible.

#### Scenario: An existing folder differing by case is reused

- **WHEN** `projects/kinkverse/` exists and `create_note` is called with `projects/KINKVERSE/x.md`
- **THEN** the note SHALL be written at `projects/kinkverse/x.md` and no `KINKVERSE` folder SHALL be created
- **AND** the response SHALL say the existing folder was reused

#### Scenario: A file differing only by case is refused

- **WHEN** `Plan.md` exists and `move_note` or `create_note` targets `plan.md`
- **THEN** the tool SHALL refuse with `path_case_collision` naming `Plan.md`
- **AND** both the source and `Plan.md` SHALL be unchanged
