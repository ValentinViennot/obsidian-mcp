## ADDED Requirements

### Requirement: Recording a change event SHALL NOT alter a write tool's result, outcome or ordering guarantees
A write tool SHALL record its change event only after its confirmed publication has returned, so that no event write can sit between `_confirmed_publication`'s confirming read and the publication. Where the tool already writes the database for the mutation (today only `move_note`'s `notes_metadata` path update), the event SHALL be inserted in that same transaction, all or nothing. A failure there SHALL take the tool's existing "DB update failed; reindex will reconcile" path. Every other tool's event SHALL be appended in its own transaction. The tool's returned text, structured result, typed `BodyOutcome` and usage classification SHALL be identical whether the event write succeeds, times out on the feed lock, or fails. `move_note`'s existing partial-success warning about the database update is the one difference, and it is already part of that path. A dry run, a refused call, and a call that published nothing SHALL record no event.

#### Scenario: Dry run
- **WHEN** an agent calls `edit_note(..., dry_run=True)`
- **THEN** no event SHALL be recorded

#### Scenario: Refused precondition
- **WHEN** `edit_note` is refused because `expected_hash` does not match
- **THEN** no event SHALL be recorded

#### Scenario: Event failure is invisible to the caller
- **WHEN** `create_note` publishes and the event insert raises
- **THEN** the tool SHALL return its ordinary success text and its usage row SHALL record the ordinary success outcome

#### Scenario: move_note's event shares its metadata transaction
- **WHEN** `move_note` succeeds
- **THEN** its `moved` event and its `notes_metadata` path update SHALL be committed by the same transaction
