## ADDED Requirements

### Requirement: GitHub is the common synchronization remote
Desktop and server working clones SHALL synchronize bidirectionally through GitHub main using the inode-preserving engine and retained private conflict recovery. The desktop SHALL use one 300-second timer and the server a 120-second timer. The old bare relay SHALL remain offline as a transition archive.

#### Scenario: Desktop synchronization does not depend on the VPS network
- **WHEN** the desktop can reach GitHub directly
- **THEN** synchronization SHALL not require a VPS SSH proxy or Tailscale connection

### Requirement: Delivery promotes only the qualified immutable image
CI SHALL partition offline, schema and other PostgreSQL tests without overlap and aggregate mandatory security, Compose and contract checks. It SHALL build, scan and smoke-test one candidate per runtime execution, publish those same bytes after successful main validation, and attest exact source and digest metadata. Documentation-only changes SHALL produce no runtime image.

#### Scenario: A release fails qualification or provenance
- **WHEN** a required gate fails or the source, signer, commit or digest differs from the approved contract
- **THEN** protected GitOps intake or promotion SHALL refuse the release

### Requirement: Deployment preserves recovery and requires verified readiness
The fixed GitOps Action SHALL serialize supported writers, verify candidate settings, stop the old application, restore a complete database backup for verification, archive active working clones and recovery bytes, migrate forward, check schema and verify readiness. It SHALL then safely advance the scheduled-service checkout. A migration failure SHALL stop delivery without automatic data downgrade.

#### Scenario: Migration fails after a verified backup
- **WHEN** a migration command fails
- **THEN** the application SHALL remain stopped for explicit forward recovery
- **AND** no automatic downgrade or old-image restart SHALL occur

#### Scenario: A completed release is replayed
- **WHEN** an already selected attested release is admitted again by intake
- **THEN** promotion SHALL report unchanged without creating another release change
