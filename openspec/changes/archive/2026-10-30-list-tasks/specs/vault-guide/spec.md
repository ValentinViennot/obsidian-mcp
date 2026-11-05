## ADDED Requirements

### Requirement: Guide points at `list_tasks` for task queries

The primer returned by `get_vault_guide` SHALL state, in its Tasks section and in neutral framing, that `list_tasks` lists checkbox tasks across the vault (open, done or cancelled, with Tasks-plugin dates) from the index without reading notes, and that Tasks-plugin query blocks are literal text this server does not evaluate. The server instructions SHALL name `list_tasks` in one sentence outside the 512-character lead, and SHALL stay within the 2,000-character cap.

#### Scenario: The primer names list_tasks

- **WHEN** `get_vault_guide` is invoked
- **THEN** the primer's Tasks section SHALL mention `list_tasks` and SHALL say that query blocks are not evaluated
- **AND** the wording SHALL NOT use "MUST", "IMPORTANT" or an equivalent imperative to rank it above other tools

#### Scenario: The instructions name list_tasks within their caps

- **WHEN** the server's `instructions` are inspected
- **THEN** they SHALL contain `list_tasks`, the first 512 characters SHALL be unchanged by this change, and the total SHALL be at most 2,000 characters
