## ADDED Requirements

### Requirement: Authorizations require a connection alias
Every OAuth authorization SHALL require an editable application-prefilled alias, normalized with NFC and trimming, of 1–128 characters without control/format characters or Git delimiters. DCR and CIMD codes and token families SHALL preserve that alias unchanged through refresh. Migration044 SHALL invalidate unnamed legacy OAuth credentials and exchangeable codes while retaining API keys.

#### Scenario: Authorize and refresh a named connection
- **WHEN** a user approves a valid alias and later refreshes its credentials
- **THEN** writes SHALL retain the original connection alias and stable technical email
- **AND** the panel SHALL distinguish the alias from the application name

#### Scenario: Legacy credentials cannot write under a default identity
- **WHEN** an unnamed legacy OAuth token or code is used after migration044
- **THEN** authentication or exchange SHALL fail and require a new authorization

### Requirement: Request context determines Git authorship
Writes SHALL use the connection alias as Git author and an opaque-identifier-derived technical email. API keys SHALL use their valid names; invalid names SHALL prevent writes until corrected. The server SHALL retain its configured technical committer without changing global Git configuration. MCP tools SHALL expose no author parameter.

#### Scenario: Concurrent connections remain isolated
- **WHEN** two named connections write concurrently
- **THEN** each commit SHALL carry its own author and the configured server committer without identity contamination

### Requirement: Deferred uploads retain their originating author
Upload capabilities SHALL snapshot connection authorship when minted and use it when committing published files. Git failure SHALL not remove published files; later recovery SHALL use an explicit technical identity.

#### Scenario: Upload publication outlives its request
- **WHEN** a named connection creates a capability and publication completes later
- **THEN** the resulting commit SHALL use the captured author
- **AND** a failed Git commit SHALL leave published bytes available
