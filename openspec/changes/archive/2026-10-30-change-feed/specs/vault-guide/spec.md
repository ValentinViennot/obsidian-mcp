## ADDED Requirements

### Requirement: The guide SHALL describe the change-feed cursor convention in neutral framing
The served primer SHALL contain one line that names `get_changes`, says that `get_changes(since=<stored cursor>)` lists what changed in the vault since the cursor, and says to store the returned `next_since` in the agent's own notes for the next session, and SHALL point to `order_by="content_changed"` with `since` on `get_recent` / `list_notes` for notes whose content changed. The line SHALL use the neutral framing the other guide references use. It SHALL NOT contain "MUST", "IMPORTANT" or an imperative to call the tool before any other.

#### Scenario: The guide mentions the feed
- **WHEN** `get_vault_guide` is invoked
- **THEN** the primer SHALL mention `get_changes`, `next_since` and `order_by="content_changed"`
- **AND** the primer section SHALL remain byte-identical between a vault with `CLAUDE.md` and one without

#### Scenario: Neutral framing
- **WHEN** the primer line naming `get_changes` is inspected
- **THEN** it SHALL NOT contain "MUST", "IMPORTANT" or "call … first"
