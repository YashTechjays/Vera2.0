# ADR-0002: UUIDv7 primary keys, native uuid columns, client-side generation

Date: 2026-06-10 · Status: Accepted

## Context

Multi-tenant PHI data with: B-tree index locality concerns (UUIDv4 writes
scatter), the need to mint IDs before flush (audit records, room names,
cross-process references), and a hard requirement that identifiers are not
guessable-sequential across tenants (bigserial leaks volume and ordering).

## Decision

Every table's primary key is a UUIDv7, stored as the native Postgres `uuid`
type (never text), generated **client-side** via `uuid-utils`
(`vera_core.db.uuid7`), provided by a single `UUIDv7PKMixin` /
`TenantScopedMixin` so the id + tenant_id + timestamps shape is uniform.

## Rationale

- **UUIDv7 over v4**: time-ordered prefix keeps B-tree inserts append-ish
  (less page splitting, better cache locality) and gives free coarse
  creation-time ordering.
- **Native uuid over text**: 16 bytes vs 36+, correct comparisons, and RLS
  policies compare `uuid = uuid` without casts.
- **Client-side over in-DB**: `gen_uuidv7()` requires Postgres 18; Cloud SQL
  pinning to a DB major version for key generation is a bad trade. Client-side
  IDs also exist before INSERT (needed by seeding, tests, and the audit path)
  and work without a DB in unit tests.

## Consequences

- The ~48-bit timestamp prefix leaks row creation time to anyone who can read
  the id; acceptable (ids are not secrets, and RLS bounds who reads them).
- All inserts must go through the ORM/mixin default (raw SQL inserts must call
  uuid7() themselves).
