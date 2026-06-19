# DevOps TODO — infrastructure obligations the app code cannot enforce

Some HIPAA / security guarantees live in the deployment, not in the codebase. When
a change in this repo *depends on* an infra property — a clock being synced, a key
being CMEK-backed, a role existing — record it here so it is tracked and verified,
not assumed. Each item names *what*, *why it matters*, and *where the dependency
came from*.

Status legend: ☐ open · ◐ in progress · ☑ done (link the PR / runbook / ticket).

| # | Item | Why it matters | Source |
|---|------|----------------|--------|
| 1 | ☐ **Verify the Cloud SQL Postgres host clock is NTP-synced**, and confirm the instance is *managed* Cloud SQL (not self-managed Postgres on a VM). Add clock-drift monitoring/alerting where available. | Postgres `now()` is the **single clock of record** for every `created_at`/`updated_at` and every audit + elevation timestamp (`audit_log`, `auth_audit_log`, `tenant_elevation`). HIPAA audit-trail integrity (§164.312(b)) depends on that clock being accurate and monotonic. Managed Cloud SQL hosts are NTP-synced by Google; a self-managed host is not, unless configured. | Timestamp audit (2026-06-17); reinforced by the 0004 migration that moves elevation expiry computation onto the DB clock. |
| 2 | ☐ **Provision a Cloud KMS key ring and symmetric encryption key for MFA envelope encryption**, grant the GKE workload-identity service account `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the specific key, and set `VERA_KMS_KEY_NAME` (full resource path: `projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}`) in the GKE deployment env. Without this, `build_kms` falls back to `LocalDevKMS` and startup will fail with a `ValueError` (`LOCAL_KMS_MASTER_KEY` not set). | MFA TOTP seeds are envelope-encrypted at rest: the per-row DEK is wrapped by Cloud KMS (`GCPCloudKMS`) into `user_identity.totp_dek_ct`, and the seed ciphertext lives in `user_identity.totp_seed_ct`. Key rotation is forward-safe: `totp_key_ref` stores the version that wrapped each row's DEK; Cloud KMS `decrypt` selects the correct version automatically. | MFA DB envelope encryption (2026-06-19). |

## How to use this file

- Add a row when a code change introduces (or surfaces) an infra dependency.
- Keep rows even after they're done (mark ☑ + link the evidence) — the audit trail
  of "we checked this" is itself useful for compliance review.
- This is an operational checklist, not an ADR; architectural *decisions* still go
  in a numbered ADR.
