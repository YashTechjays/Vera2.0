# Platform super-admin: browser-based MFA enrollment on first login

**Date:** 2026-07-08
**Branch:** `feat/platform-mfa-enroll` (off `dev`)
**Status:** design approved, pending spec review

## Problem

Today a platform super-admin's MFA is enrolled by the `bootstrap_platform_admin.py`
script, which prints the `otpauth://` QR **once in the terminal**. Whoever runs the
deploy must capture it from the console and hand it to the operator. Clunky, and the
secret transits an ops channel.

Tenant users, by contrast, already get a clean **first-login enrollment wall** in the
browser: an enforced-but-unenrolled user who signs in is shown the QR on screen,
scans it, confirms one code, and is enrolled + logged in. This spec brings that same
flow to the platform (super-admin) login.

## Goal

On first `/platform/login`, an operator whose MFA is not yet set up sees the QR in the
**browser**, scans it, confirms a code, and is enrolled + logged in — no terminal QR.

## Non-goals

- No change to tenant login (already has this).
- No terminal-QR fallback (decided: browser-only path). The bootstrap simply stops
  enrolling MFA; it no longer prints a QR.
- No platform-operator *invite* flow (adding operator #2+) — separate work.
- No "reset my lost authenticator" flow — out of scope for v1 (recovery is re-bootstrap
  against the DB, unchanged).

## Design

Mirror the existing tenant enrollment-wall pattern (`api/v1/auth.py`) onto platform
auth (`api/v1/platform_auth.py`). Four changes:

### 1. Bootstrap — stop enrolling MFA
`scripts/bootstrap_platform_admin.py`: create the operator's `user_identity` with
`mfa_enabled=False` and **no** seed. Remove the `mfa.enroll(...)` call and the QR
printout. The operator now finishes MFA setup in the browser.

### 2. Platform login — return an "enroll" challenge when unenrolled
`POST /platform/auth/login` currently always returns `mfa="verify"` (it assumes MFA is
enrolled). Add the tenant branch: after the password check, if the identity's
`mfa_enabled` is False, mint the TOTP seed now (`mfa.enroll`), store an enrollment
challenge in the `MFA_ENROLL_NS` namespace, and return
`mfa="enroll", mfa_token=<challenge>, provisioning_uri=<otpauth uri>`. If MFA is already
enrolled, behave exactly as today (`mfa="verify"`).

### 3. New endpoint — confirm the first code + finish setup
`POST /platform/auth/mfa/enroll-activate`: mirror the tenant `mfa_enroll_activate`.
Unauthenticated, gated by the enrollment `mfa_token` from step 2. Confirms a live TOTP
code against the seed, sets `mfa_enabled=True`, mints the platform session
(`tenant_id=None`, `account_type='platform'`), returns recovery codes once. On a wrong
code: audited `LOGIN_FAILURE`, 401, no session.

### 4. Frontend — render the QR on the platform login
- `authSlice.platformLoginThunk`: branch on the response like `loginThunk` does — when
  `res.mfa === "enroll"`, set the MFA state to `{ step: "enroll", platform: true,
  provisioningUri }` (the state already supports these fields).
- Add `platformEnrollActivateThunk` calling the new endpoint.
- The existing MFA-enroll screen (used by tenants) renders when `step === "enroll"`;
  route its activate call to the platform endpoint when `platform` is set.

## Data flow

```
DevOps: just bootstrap-platform <email> <password>
        -> operator created, mfa_enabled=False, NO terminal QR

Operator browser: /platform/login  (email + password)
   POST /platform/auth/login
        -> mfa="enroll", mfa_token, provisioning_uri
   Browser shows QR (from provisioning_uri)
   Operator scans, enters 6-digit code
   POST /platform/auth/mfa/enroll-activate  (mfa_token + code)
        -> session_token + recovery_codes (shown once)
        -> mfa_enabled now True

Next login: POST /platform/auth/login -> mfa="verify" (asks for code only)
```

## Error handling

- Wrong password: unchanged — uniform 401, constant-time verify.
- Wrong first code at enroll-activate: 401, audited `LOGIN_FAILURE`, no session, seed
  remains so the operator can retry with a correct code.
- Expired enrollment challenge (TTL): 401; operator re-logs in to get a fresh QR. Note:
  re-login before enrollment re-mints the seed, so only the latest QR is valid.
- Already-enrolled operator hitting enroll-activate: rejected (no enrollment challenge).

## Security notes

- The QR is shown only after a correct password — same trust level as the tenant
  enrollment wall. The operator holds the password already.
- MFA remains mandatory: no session is minted until a live code is confirmed. Removing
  the terminal QR does not weaken the control; it moves where the secret is displayed.
- Bootstrap stays idempotent and no-op-if-exists (unchanged).

## Testing

Backend (mirror the tenant enroll tests):
- first platform login (unenrolled) returns `mfa="enroll"` + a provisioning_uri
- enroll-activate with a valid TOTP code mints a session + recovery codes, sets
  `mfa_enabled=True`
- enroll-activate with a wrong code: 401, no session, audited failure
- a second login now returns `mfa="verify"`
- bootstrap creates the operator with `mfa_enabled=False` and prints no QR

Frontend:
- platform login returning `mfa="enroll"` renders the QR screen
- activating routes to the platform enroll-activate endpoint

## Rollout note

Deploy docs change: the "capture the QR from the terminal" step goes away. New note:
"run bootstrap, then the operator sets up 2FA in the browser at /platform/login."
