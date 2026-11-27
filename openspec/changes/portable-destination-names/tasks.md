# Implementation

- [x] `src/services/portable_path.py`: pure `normalize_destination` and `portable_component`.
- [x] Declare `unportable_path` and `path_case_collision` in `src/services/refusals.py`.
- [x] Wire the destination of `create_note`, `move_note`, `write_file`, `request_upload`, `import_from_url`; listing siblings through the vault root the write uses.
- [x] Response line `path_normalized:`; commit message note through `git_vault.record_note`.
- [x] Tool descriptions in `src/mcp_server/server.py`.
- [x] `tests/test_portable_destination_names.py`: pure rules and tests per wired tool.

# Qualification

- [x] Offline suite in the Linux test container.
- [ ] Integration (PostgreSQL) partitions: CI only, not run on the authoring Mac.
- [ ] End-to-end exercise of the five tools against the live server after deploy.
