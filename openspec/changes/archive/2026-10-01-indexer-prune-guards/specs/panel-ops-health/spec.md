## MODIFIED Requirements

### Requirement: `/health` SHALL report indexer degradation without disclosing paths or tenants

The `/health` response SHALL include an `indexer` object carrying `status` (`ok`, `degraded` or `disabled`), `task_running`, `failing_scopes` (the number of distinct scopes whose index, incomplete-re-derive or incomplete-walk failure count is at or above `INDEXER_DEGRADED_AFTER_FAILURES`), `embedding_failing_scopes` (likewise for embedding failures, counted as in the index-integrity failure-accounting requirement), `max_consecutive_failures`, `quarantined_notes` (quarantined notes across all scopes) and `last_success_at` (the most recent successful index pass of any scope, or null). `indexer.status` and the top-level `status` SHALL be `degraded` when the indexer task is recorded as not running, when any index, embedding, incomplete-re-derive, incomplete-walk or enumeration failure counter is at or above the threshold, or when `quarantined_notes` is greater than zero; `indexer.status` SHALL be `disabled` when indexing is intentionally not run (`MCP_SANDBOX_MODE`), which SHALL NOT by itself make the top-level status `degraded`. The HTTP status code SHALL remain 200 in every case, so liveness and readiness probes are unaffected. The response SHALL be computed from in-process state only, without probing, and SHALL NOT contain any path, error message, SQLSTATE or user identifier.

#### Scenario: A healthy indexer reports ok

- **WHEN** the indexer task is running, no counter is at the threshold and nothing is quarantined
- **THEN** `/health` SHALL return 200 with `status` `ok` and `indexer.status` `ok`

#### Scenario: A repeatedly failing indexer reports degraded

- **WHEN** a scope's index pass has failed `INDEXER_DEGRADED_AFTER_FAILURES` times in a row
- **THEN** `/health` SHALL return 200 with `status` `degraded`, `indexer.failing_scopes` at least 1 and `indexer.max_consecutive_failures` at least that count

#### Scenario: An embedding outage reports degraded

- **WHEN** a scope's embed passes have reported per-note embedding failures (or the embed stage raised) `INDEXER_DEGRADED_AFTER_FAILURES` times in a row
- **THEN** `/health` SHALL report `status` `degraded` and `indexer.embedding_failing_scopes` at least 1

#### Scenario: A dead indexer task reports degraded

- **WHEN** the indexer background task has ended by exception while the application keeps serving
- **THEN** `/health` SHALL return 200 with `status` `degraded` and `indexer.task_running` false

#### Scenario: A quarantined note reports degraded without naming it

- **WHEN** one note is quarantined
- **THEN** `/health` SHALL report `status` `degraded` and `indexer.quarantined_notes` 1
- **AND** the response body SHALL NOT contain the note's path or its owner's identifier

## ADDED Requirements

### Requirement: An administrator SHALL be able to confirm that a vault was emptied on purpose

The control panel SHALL offer administrators a "Confirm vault is empty" action — in the settings Danger zone in single-user mode, and on each user's edit page in multi-user mode — that grants that scope the single-use, 15-minute empty-prune permission defined by index-integrity and starts a background index pass for that scope only, leaving every other scope untouched. The action SHALL require a confirmation token, issued by a confirmation page that states how many indexed notes will be deleted if the vault is empty, that is signed for this action alone and binds the target scope, the issuing administrator and the scope's vault assignment at issue time; redemption SHALL refuse, granting nothing, a token that is unsigned or signed for another action, older than ten minutes, issued before the server last restarted, already redeemed, redeemed by a different administrator, posted for a different scope, or issued under a different vault assignment. The action SHALL be refused, granting nothing, for a user who is inactive, deleted, has no vault assigned, or is quarantined by the vault-overlap check. The control SHALL follow the panel's content-security rules: no inline handlers or style attributes, a `type="button"` confirm control that fails closed. Granting SHALL be logged at WARNING with the administrator's username and the scope. Non-administrators SHALL NOT see or reach the action.

#### Scenario: An administrator confirms an emptied vault

- **WHEN** an administrator opens the confirmation page for a scope whose index holds 50 notes and confirms with the token it issued
- **THEN** the page SHALL have stated that 50 notes will be deleted, the scope SHALL hold an empty-prune permission, and a background pass for that scope alone SHALL have been started

#### Scenario: A replayed, forged or misdirected confirmation grants nothing

- **WHEN** the confirm request carries a token that was already redeemed, has expired, was not issued by the server for this action, was issued for another user's vault or by another administrator, was issued before the vault assignment changed, or was issued before the server restarted
- **THEN** no permission SHALL be granted and no pass SHALL be started

#### Scenario: An ineligible target is refused

- **WHEN** an administrator confirms for a user who is inactive or quarantined
- **THEN** no permission SHALL be granted and no pass SHALL be started

#### Scenario: A non-administrator cannot confirm

- **WHEN** a non-administrator requests the confirmation page or posts the confirm action
- **THEN** the request SHALL be refused

#### Scenario: Health counts a walk-only degradation once

- **WHEN** one scope's only degraded counter is `walk_incomplete` at the threshold
- **THEN** `/health` SHALL report `degraded` with `failing_scopes` equal to 1
