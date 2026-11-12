# Synchronize the November upstream task and connector improvements

## Why

Bring the maintained fork through upstream `aafb9f0` while keeping its deployed
database history, named connection attribution and recoverable vault syncing.

## Scope

Merge all six upstream commits after `7e8c992`, append new migrations as
045–047 after fork 044, advance the fork extraction/classification markers,
and qualify the combined tool contract. The active Ollama configuration and
model remain unchanged. There is no production deployment in this change.

## Impact

The MCP surface gains task agenda and three one-line task mutations. A user's
Tasks card controls task rules and dates. CIMD clients can authenticate with
private-key assertions. The optional TEI provider is available without being
selected. Existing fork features remain in the same workflows.

See `docs/architecture/upstream-sync-20261112.md` for the compatibility contract
and migration map. Delivery work is tracked in CIO card #3622.
