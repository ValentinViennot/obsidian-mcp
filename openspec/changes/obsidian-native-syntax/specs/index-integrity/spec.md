## ADDED Requirements

### Requirement: An extraction-version bump states what it costs

Every bump of `CURRENT_EXTRACTION_VERSION` SHALL declare, in the constant's own
comment and in the change that carries it, whether it moves the **embedded
text** and for which notes — because that, and not the link or tag derivation,
is the only part of a bump that costs provider calls.

A bump SHALL register its predecessor's cleaning function as a frozen entry in
the per-version registry rather than re-pointing the old key at the current
cleaner. Re-pointing would make every previously stamped row compare equal
against a function it never ran, which for a bump that *does* move the embedded
text silently certifies stale vectors, and for one that does not would still
destroy the ability to roll the grammar back.

Where a bump moves the embedded text for a **subset** of notes, invalidation
SHALL remain the per-note cleaned-output comparison and SHALL NOT be widened to
a blanket clear: the comparison is what scopes a re-embed to the notes that
actually changed rather than to the whole vault.

A bump SHALL NOT require an operator to run `make reindex` or
`make rebuild-tsvectors`. The marker exists precisely so that the next ordinary
pass treats every stale-marked row as changed — re-parsed, re-tagged,
re-linked, keyword vector rewritten, marker re-stamped.

#### Scenario: A bump that moves the embedded text for some notes

- **WHEN** `CURRENT_EXTRACTION_VERSION` moves to a version whose cleaner removes a construct the previous one kept
- **THEN** `embedded_content_hash` SHALL be cleared for exactly the notes containing that construct
- **AND** every other note SHALL keep its vectors and make no provider call on account of the bump

#### Scenario: The predecessor's cleaner stays callable

- **WHEN** a row is stamped with any version this build has ever shipped
- **THEN** `clean_at_version` SHALL return that version's own output for it, never the current version's

#### Scenario: No manual reindex is required

- **WHEN** a build carrying a bump is deployed
- **THEN** the next index pass SHALL re-derive links, tags and the keyword vector for every note in scope without any operator command
