# Sprint 2 Defect Status Report

Date: 2026-07-14 · Fixes branch: `fix/sprint-2-defects` (PR #83) · Verified: backend `just check` 1031 passed, frontend tsc/eslint/vitest 199 passed, plus local Playwright pass on the running app.

Legend: ✅ Fixed (PR #83) · ☑️ Already fixed on `dev` — retest on a current build · ✔️ Not a defect · 🏷️ Product decision · 🛠️ Ops/data fix (no code) · ⏸️ Deferred · ❓ Untriaged (new)

## User management & invitations

| Defect | Status | Note |
|---|---|---|
| Copy icon not working in the User Invitation URL | ✅ Fixed | Clipboard fallback now works inside the invite dialog |
| "Action" column header not aligned (User Invitation page) | ✅ Fixed | Header/cell padding aligned — Playwright PASS |
| Duplicate eye icons in password field | ✅ Fixed | Native browser reveal icon suppressed — Playwright PASS |
| User account can be created with an invalid email address | ✅ Fixed | Backend RFC email validation (422 on malformed) — API-verified |
| User invitation link remains usable after account activation | ✔️ Not a defect | Prod uses the single-use Redis invitation store; only an env running the test-only in-memory store can reproduce this (deploy-config check) |
| Invitation URL shows "Set Password" for a deactivated user | ✅ Fixed | New invite-validation pre-flight shows "account deactivated" instead of the form — Playwright PASS (end-to-end) |
| Password setup page shown again via browser Back after activation | ✅ Fixed | History replace + no-store — Playwright PASS (end-to-end) |

## VA role, login & session

| Defect | Status | Note |
|---|---|---|
| Agent profile displayed instead of VA profile after VA login | ☑️ On `dev` | Sidebar now driven by `/auth/me` — retest on a current build |
| Data Management not accessible after VA login | ✅ Resolved (PR #82) | Was by-design (VA had only Voice Lab); PR #82 on `dev` now grants the VA role Live Monitoring + Data Management |
| Generic error message for deactivated VA login | ☑️ On `dev` | Backend returns 403 + clear message; FE error handling fixed — retest on a current build |
| Session timeout warning popup with countdown | ☑️ On `dev` | Countdown dialog exists; deadline is server-driven — retest on a current build |

## Platform operator

| Defect | Status | Note |
|---|---|---|
| Platform Operator redirected to Tenant Admin login after logout | ✅ Fixed | Logout now redirects by account type (`/platform/login` vs `/login`) |
| Platform Operator can sign in using an expired OTP | ✅ Fixed | TOTP codes are now single-use; replay within the drift window is rejected |
| Tenant Access duration field appends input instead of replacing | ✅ Fixed | Field selects its content on focus |

## Voice / AI pipeline

| Defect | Status | Note |
|---|---|---|
| AI does not recognize interruption and repeats the question | ⏸️ Deferred | Config change documented (barge-in threshold + no TTS resume); must be validated on real calls before committing |
| AI response delayed after transfer to human agent | ⏸️ Deferred | TTS pre-warm / filler during the agent swap; needs live-call validation |
| Deactivate msg showing for a long time | ✅ Fixed | Dismiss (×) button added — Playwright PASS |
| AI fails to detect an incorrect Member ID from the IVR | ⏸️ Deferred | PHI-wall design gap — the model never sees the real ID, so it can't compare. Two PHI-safe designs written up (server-side verify tool recommended); overlaps with PR #81 — needs one combined sign-off |
| Provider details not refreshed on Voice Lab after toggling | ✅ Fixed | Provider list refetched when the IVR toggle turns on — Playwright PASS |
| No response from both IVR and AI on the Voice Lab | ✅ Fixed (client half) | Clear "agent hasn't connected" warning after 15s — Playwright PASS. Server-side agent-ready signal tracked as follow-up |
| Deactivating a provider — must its IVR playbook be deactivated too? | ✔️ Not a bug — answer: **No** | Dispatch already excludes an inactive provider's playbook automatically; it re-arms when the provider is reactivated |
| AI conversation displayed under the caller in the transcript | ✅ Fixed | Transcript attributes by speaker source (bot/rep); needs a live call to observe |

## IBV form UI

| Defect | Status | Note |
|---|---|---|
| Show the date format in DOB/date fields | ✅ Fixed | Format hint now shown even for fields with an "N/A" default — Playwright PASS |
| Patient Gender field displayed in date format | 🛠️ Ops/data | Stale schema version on that environment — re-run schema compile + seed and re-point affected forms; code is correct (renders as dropdown after reseed — Playwright PASS) |
| Highlight prerequisite fields with a distinct color | ✅ Fixed | Appointment Date/Type + Callback Number render amber with a legend entry — Playwright PASS |
| Remove prerequisite highlight from Spouse Gender | ✅ Fixed | Stays green (voice-agent context), excluded from prerequisite amber — Playwright PASS |

## IBV form after upload

| Defect | Status | Note |
|---|---|---|
| Diagnostic Testing copay/coinsurance show no reference values | ✅ Fixed | Gated-off rows now show $0 / 0% — Playwright PASS |
| Placeholder text instead of actual values after upload | ✅ Fixed | Upload now rejects (422) payload paths that don't match the schema — root cause of dead values |
| Tooltip repeats multiple times on "Center of Excellence Required" hover | ✅ Fixed (PR untriaged-branch) | A redundant native `title` on the field label cell duplicated the Radix reason tooltip → two tooltips per hover. Removed it — Playwright PASS (exactly one tooltip on hover) |
| Form saves even when mandatory prerequisite fields are cleared after upload | ✅ Fixed (PR untriaged-branch) | Save is blocked (button + guard) when the reviewer empties a required, no-default field that had a value on load. Computed from schema requiredness, so a form that arrived incomplete/format-quirky still saves — Playwright PASS. NOTE: of the 3 "prerequisite" fields only Appointment Date is `required` with no default; Appointment Type has an `N/A` default and Callback Number isn't `required` — making all three hard-mandatory is a separate schema decision |
| Warning message shown only for small group and self-insured policy | ✔️ Working as designed — needs product confirmation | It's an intentional contradiction rule (`small_group_self_insured_conflict`) that fires only for the one implausible combo (Small Group + Self Insured), prompting re-verification. The other 3 combos are valid and correctly don't warn. If product wants additional combos to warn, add a `Contradiction` entry + recompile — no code bug |

## Call status

| Defect | Status | Note |
|---|---|---|
| "In queue" status displayed too long in Data Management after call end | ✅ Fixed (PR untriaged-branch) | The worklist fetched once and never auto-refreshed, so a server-side status change (call end → post-call eval → completed/exception_review) never surfaced until a manual reload. Added a 30s poll — Playwright PASS (badge auto-updated from Ready For Processing to Exception Review within the poll window, no interaction) |

## Totals

- ✅ **22 fixed** (18 in PR #83, 1 via PR #82 role broadening, 3 in the untriaged-fixes branch)
- ☑️ **3 already fixed on `dev`** — retest on a build that includes them
- ✔️ **3 not a defect / working as designed** (invite-link reuse; provider/playbook deactivation; small-group/self-insured warning — last one pending product confirmation)
- 🛠️ **1 ops/data** (schema reseed on the affected environment)
- ⏸️ **3 deferred** — voice-pipeline changes needing live-call validation or a design sign-off

The 3 newly-fixed untriaged items were RCA'd, fixed, and Playwright-verified on the local app (branch `fix/sprint-2-untriaged`, off `fix/sprint-2-defects`). Full RCA + per-fix commits + Playwright details: `docs/qa/sprint2-defect-rca.md`.
