# Sprint 2 Defect List — RCA & Fix Analysis

Analyzed against branch `dev` (2026-07-13). Classification: **BE** = backend, **FE** = frontend. Effort: S/M/L.
Note where relevant: several defects are already fixed on `dev` but QA may be testing a build cut before those commits — those are marked **[verify build]**.

## User management & invitations

| # | Defect | RCA | Fix | Class | Effort |
|---|---|---|---|---|---|
| 1 | Copy icon not working in invitation URL | The dialog uses the shared `copyText` helper (`vera-frontend/src/lib/clipboard.ts`), which has the HTTP fallback — but the `execCommand` fallback appends its textarea to `document.body`, outside the Radix dialog's focus trap, so `.select()` silently fails inside the invite modal and `copyText` returns false. | Append/select the fallback textarea inside the dialog content (or the Radix portal root) instead of `document.body`; surface a failed copy visibly. | FE | S |
| 2 | "Action" column header misaligned (Users page) | `Users.tsx:182` Deactivate button carries `-ml-2.5` to offset the ghost button's own padding; it over-compensates vs the header's `px-2` (8px), leaving header text and cell content ~2px apart. | Align the negative margin to the actual cell padding (`-ml-2`) or drop the correction and align header/cell via a shared padding class. | FE | S |
| 3 | Duplicate eye icons in password field | `PasswordInput` hides only Edge's native reveal (`[&::-ms-reveal]:hidden`); Chrome/Safari's built-in password affordance still renders next to our toggle. | Also suppress the webkit pseudo-element: add `[&::-webkit-credentials-auto-fill-button]:hidden` (and verify per-browser) in `components/ui/password-input.tsx:20`. | FE | S |
| 11 | Account creatable with invalid email | Backend `InviteUserRequest.email` is plain `str` (only length-checked) — `users.py:56`; FE relies on the browser's permissive `type="email"`. | Backend: `email: EmailStr` (authoritative). FE: add pattern validation in the invite dialog for fast feedback. | **Both** (BE critical) | S |
| 12 | Invite link usable after activation | Code paths are actually safe (token deleted on accept + `status != "invited"` guard + password-identity guard in `auth.py:767-798`). Most plausible env cause: the deployment uses `InMemoryInvitationStore`, which resets on restart/redeploy, resurrecting consumed tokens. | Confirm the dev/test deployment wires `RedisInvitationStore`; add a repro check. If reproducible with Redis, instrument the delete path. | BE (env/config) | M |
| 13 | Set-Password page shown for deactivated user | Backend correctly 401s (`auth.py:767-770`), but `AcceptInvite.tsx` renders the password form unconditionally on mount — no token/status pre-flight — so the user only learns on submit, with a generic message. | Add a token-validation probe endpoint (or reuse) + call it on mount and short-circuit to a clear "no longer eligible" screen; return a distinct error code for deactivated so FE can message it. | **Both** | M |
| 14 | Password page reappears via browser Back | Post-activation navigation uses `navigate(loginHref)` (push), leaving accept-invite in history; bfcache restores the filled form. | `navigate(loginHref, { replace: true })` (AcceptInvite.tsx:69,84) + the mount-time probe from #13; add `Cache-Control: no-store` on the accept endpoint. | FE (primary) + BE (no-store) | S |

## VA role, login & session

| # | Defect | RCA | Fix | Class | Effort |
|---|---|---|---|---|---|
| 4 | Agent profile shown instead of VA profile | Sidebar footer was a static "Agent View" placeholder; fixed on `dev` (commits 970c288 + 14b6312 drive it from `/auth/me`). **[verify build]** Remaining nit: the role name (VIRTUAL_ASSISTANT) is still not displayed — secondary line shows email/account type. | Confirm QA build includes the fix; optionally surface `user.roles[0]` as the secondary line. | FE | S |
| 5 | Data Management not accessible after VA login | **By design.** VIRTUAL_ASSISTANT holds only `voice_lab:sandbox` (`rbac_defaults.py:73`); Data Management gates on `forms:read` (nav + API). Separate real bug on older builds: Voice Lab nav gated on `calls:read`, hiding it from VAs — fixed on `dev`. **[verify build]** | If product wants VAs in Data Management: grant `forms:read` to the role (rbac_defaults + seed migration) — product sign-off needed. Otherwise close as designed. | BE (if granted) | M |
| 6 | Generic error for deactivated VA login | Two-part: backend used to hide deactivated users behind a uniform 401 (fixed by 66774c4: 403 + clear message after password proof); FE `loginThunk` stripped `httpStatus` during Redux error serialization so `instanceof ApiError` never matched → generic fallback (fixed on dev via `serializeApiError`). **[verify build]** | Confirm the QA build; if still reproducible on dev-current, check the MFA-step path uses the same error mapping. | **Both** | S |
| 15 | Session-timeout warning popup (enhancement) | Already built: `IdleWarningDialog` + `IdleManager` show a 60s countdown (`WARNING_LEAD_MS`, `lib/auth/idle.ts:8`). On older builds the deadline came from client-side constants and drifted from the server cap → silent 401 with no warning; `dev` (653e61b) drives it from `/auth/me`. **[verify build]** | Verify build; if warning still missed, instrument `computeIdleState` phases. | FE+BE (already done on dev) | M (backport) |

## Platform operator

| # | Defect | RCA | Fix | Class | Effort |
|---|---|---|---|---|---|
| 16 | Redirected to tenant login after platform logout | Three hardcoded `"/login"` destinations (`Topbar.tsx:17`, `IdleManager.tsx:49`, `RequireAuth.tsx:12`); by the time they run, `state.user` (and `account_type`) is already cleared, so there's nothing to branch on. | Capture `logoutPlane` in `authSlice` before wiping the user (both `forceLogout` and `logoutThunk.fulfilled`), expose `selectLogoutRedirectPath`, use it at all three sites (`/platform/login` for platform sessions). | FE | S |
| 17 | Sign-in with expired OTP | `pyotp verify(valid_window=1)` at `auth/mfa.py:123` accepts codes from the previous/next 30s window (±30s), and there is **no replay protection** (no last-used-timestep tracking) — a just-rolled code still authenticates. | Set `valid_window=0` on the login `verify()` (keep 1 for enrollment if skew tolerance wanted) and add `totp_last_used_timestep` on `UserIdentity` (+migration) to reject reuse within a step. | BE | S–M |
| 18 | Tenant Access duration appends input | Controlled `type="number"` input (`TenantAccess.tsx:249-255`) with default `60`; focus places the caret at the end, so typing appends (`6030`). No select-on-focus. | Add `onFocus={(e) => e.target.select()}` to the duration input. | FE | S |

## Voice / AI pipeline

| # | Defect | RCA | Fix | Class | Effort |
|---|---|---|---|---|---|
| 7 | AI ignores interruption, repeats question | Worker interruption config (`cascade.py`): `min_duration=0.5s` swallows short barge-ins, and `resume_false_interruption=True` resumes the paused TTS instead of taking a fresh LLM turn — so the pending question replays. IVR agent adds `min_words=3`. | Lower `min_duration` to ~0.2–0.3s, set `resume_false_interruption=False`; optional prompt rule against re-asking the immediately-preceding question. Tune with real calls. | BE (worker) | S |
| 8 | AI response delayed after transfer to human | `transfer_to_verification()` does a sequential agent swap: IVR agent teardown → turn-detection model re-init → `VeraAgent.on_enter` → full LLM+TTS cold start for the greeting = 2–5s of silence. | Pre-warm the Cartesia TTS connection, mask the swap with a short break/filler, or pre-generate the greeting before the swap completes. | BE (worker) | M |
| 9 | "Deactivate msg showing for a long time" | Most plausible: Users-page deactivation notice — 5s auto-dismiss timer, no close button (`Users.tsx`); rapid successive deactivations reset the timer, making it look stuck. | Add a dismiss (×) button; keep/adjust the auto-dismiss constant. Ask QA to confirm the screen if this isn't it. | FE | S |
| 10 | AI fails to detect incorrect Member ID from IVR | **Design gap, not a bug**: the PHI wall keeps the real member ID out of the navigator prompt (`ivr_prompt.py` uses a literal placeholder), so the LLM has no ground truth to compare the IVR's read-back against — it confirms by default. | Needs a design decision: (a) a server-resolved `verify_member_id(read_back)` function tool (real ID never enters the prompt), or (b) token hydration at the DTMF/speech edge. | BE | L |
| 19 | Provider details not refreshed on Voice Lab after toggling | Provider list is fetched once on mount (`VoiceLab.tsx:245-255`, empty-dep `useEffect`, "Load selectable providers once") — changes made in Settings (or activation toggles) never reflect until a hard reload. | Refetch on the IVR toggle flipping on and/or on window focus; or refetch each time the picker opens. | FE | S |
| 20 | No response from IVR and AI in Voice Lab | Silent multi-cause chain: (a) no agent worker connected → dispatch queues silently forever; (b) SIP carrier silently drops → worker waits full 60s `wait_for_speaker` timeout; (c) `publish_transcript` metadata missing → session runs but transcript panel stays empty; (d) missing tenant trunk → visible 409 (not this symptom). | Surface worker presence in the Voice Lab UI (poll participants / reuse the sweeper's no-agent signal) with an explicit "agent not connected" warning; check worker logs + `vera:transcript:*` keys when triaging. | BE + FE | M |
| 21 | Deactivating provider — must playbook be deactivated too? | **No.** `ivr_selection.py` joins on `IvrPlaybook.status == ACTIVE AND InsuranceProvider.status == ACTIVE` — an inactive provider's playbook is automatically excluded at dispatch, and inactive providers disappear from the picker. Playbooks stay for history and re-arm when the provider is reactivated. | Answer "no" to QA. Optional UX: show a "provider inactive — playbook won't be used" badge on the playbook admin screen. | None (FE badge optional) | S |
| 22 | AI conversation displayed under the caller in transcript | Voice Lab's `TranscriptPanel` attributes by `role` only, and the `source` field (`bot`/`rep`) is dropped during SSE deserialization (`services/transcription.ts`); Live Monitoring's `CallTranscript.tsx` does it correctly via `source`. Secondary worker-side risk: `on_agent_item` role filter could misroute unusual SDK events into the user path. | Add `source` to the FE `TranscriptEvent` type and attribute like `CallTranscript.tsx`; add a worker test pinning the SDK event shape. | FE (primary) | S |

## IBV form

| # | Defect | RCA | Fix | Class | Effort |
|---|---|---|---|---|---|
| 23 | Show date format in DOB/date fields | Largely done (`c50e87c` renders `validation.date_format`). Residual: fields with `default="N/A"` (e.g. spouse DOB) eclipse the format hint because `hint = placeholder ?? field.default` (`FieldRenderer.tsx:114`). | For date leaves, prioritize `date_format` over `default` in the hint chain. | FE | S |
| 24 | Patient Gender rendered as date | Catalog and compiled artifact are correct (`type="enum"`); the env is serving a **stale `schema_version` row** (form pinned to an old version with the wrong type). | Re-run `just compile-schemas && just seed-schemas` on the env; re-point/reupload affected forms. No code change. | BE (ops/seed) | S |
| 25 | Highlight prerequisite fields distinctly | All three (Appointment Date/Type, Callback Number) are `system_fields` → same violet as 15 other system fields; there is no "prerequisite" usage category (`usageMeta.ts`, `schema.ts:fieldUsageOf`). | Add a `prerequisite_fields` DSL key (catalog lists the three paths) + a new FE usage category with a distinct color + legend entry. | **Both** | M |
| 26 | Remove prerequisite highlight from Spouse Gender | Spouse Gender is `role="context"` → green (context) highlight, not the system violet; QA is reading the green as "prerequisite". It must stay `context` (the agent needs it for the `male_partner_in_scope` condition). | Resolves itself with #25 (distinct prerequisite color excludes it). Don't change its role. | FE (via #25) | S |
| 27 | Diagnostic Testing copay/coinsurance show no reference values | The section's CPT groups use the `"plain"` flavor whose `_INAPPLICABLE` map is empty (`authoring.py:46`), so gated-off fields get `inapplicable_value=None` → blank, unlike treatment groups ($0 / 0%). | Give the plain flavor (or a new flavor) `copay="$0"`, `coinsurance="0%"`, etc.; recompile + reseed. Audit other `"plain"` call sites. | BE (schema catalog) | S |
| 28 | Placeholder text instead of uploaded values | Path mismatch between intake payload nesting and schema paths: upload stores whatever paths `iter_leaf_answers` produces with **no validation** against the schema (`patient_forms.py:184-202`) — a double-wrapped/flat/mis-keyed payload silently stores dead paths that never match `values[path]` in the FE. | Validate produced paths against the schema's leaf paths at upload and 422 with the unknown paths; fix the intake client's payload shape. | BE | M |

## Suggested priorities

1. **Security/correctness:** #17 (OTP window/replay), #11 (EmailStr), #28 (intake path validation), #12 (invitation store config).
2. **High-visibility UX:** #16, #22, #20, #7, #4/#6 (verify build first).
3. **Product decisions needed:** #5 (VA access to Data Management), #10 (member-ID verification design), #25 (prerequisite color spec).
4. **Verify-build-first** (may already be fixed on dev): #4, #6, #15, part of #5.

---

# Fix status (branch `fix/sprint-2-defects`, 2026-07-13)

Backend gate `just check` green (1031 passed); frontend `tsc`/`eslint`/`vitest` (199) green.

## Fixed on this branch
**S (frontend):** #1 copy icon, #2 Action-column align, #3 duplicate eye icons, #9 dismiss button on deactivate notice, #14 back-nav (`replace:true`), #16 platform-vs-tenant logout redirect, #18 duration select-on-focus, #19 Voice Lab provider refetch on IVR toggle, #22 transcript `source` attribution, #23 date-format hint over `N/A` default.
**S (backend):** #11 invite email → `EmailStr` (+ dep), #27 plain-flavor CPT reference values ($0/0%) recompiled into the schema.
**M:** #13 token-scoped invite `validate` endpoint (enumeration-safe, `no-store`) + FE pre-flight so ineligible users never see Set-Password; #17 single-use TOTP within the drift window (replay rejected via `totp_last_used_timestep`; tenant path ORM, platform path SECURITY DEFINER — keeps ±30s tolerance, no `valid_window=0` regression); #25/#26 new `prerequisite_fields` DSL key + distinct amber highlight for Appointment Date/Type + Callback Number (Spouse Gender stays context/green); #28 intake upload now 422s on field paths not in the schema (root cause of "placeholder instead of values"); #20 client-side "agent hasn't joined" warning after a 15s timeout (uses LiveKit `isAgent`).

## Verified — no code change needed
- **#12** invite-link-after-activation: production wiring is `RedisInvitationStore` (persistent, single-use); `InMemoryInvitationStore` is test-only. No defect. (If a specific env resurrects tokens, it's running the in-memory store — a deploy-config check, not code.)
- **#4 / #6 / #15 / Voice-Lab nav part of #5:** already fixed on `dev` (sidebar from `/auth/me`; login error serialization; backend-driven idle timeout + warning dialog; nav gated on `voice_lab:sandbox`). Confirm QA is testing a build that includes them.

## Not bugs / product decisions (not implemented)
- **#5** VA → Data Management: **by design** (VIRTUAL_ASSISTANT holds only `voice_lab:sandbox`). Granting it requires adding `forms:read` to the role + a seed migration — product call.
- **#21** deactivating a provider: **no action needed** — the dispatch join already excludes an inactive provider's playbook, and inactive providers drop off the picker. (Optional: a "provider inactive" badge on the playbook admin screen.)
- **#24** Patient Gender as date: **environment data**, not code — the catalog/compiled schema is `type=enum`; a stale `schema_version` row is being served. Fix by `just compile-schemas && just seed-schemas` on that env and re-pointing/re-uploading affected forms.

## Deferred — need a decision or live-call validation
- **#10 (L) — AI can't detect a wrong Member ID from the IVR.** Root cause is deliberate: the real member ID never enters the navigator LLM prompt (PHI wall), so the model has no ground truth to compare the IVR's read-back against. Two PHI-safe designs to choose from:
  - **(a) Server-resolved verify tool** — add a `verify_member_id(read_back: str) -> bool` function tool to the IVR agent; the control plane compares server-side against the real ID. The LLM only ever passes the *spoken* value out; the real ID never enters the prompt. Smaller blast radius, recommended.
  - **(b) Token hydration at the audio edge** — inject an opaque token for `{member_id}` into dispatch metadata and resolve it to the real value only at the DTMF/press boundary (`hydrate_raw` seam). More faithful to the tokenization model but larger.
  Needs product/architecture sign-off before building. Est. L.
- **#7 / #8 — barge-in re-ask and post-transfer latency.** Concrete recommended config change for #7 (in `agent_worker/cascade.py`): lower interruption `min_duration` to ~0.2–0.3s and set `resume_false_interruption=False` so a detected interruption always takes a fresh LLM turn. For #8: pre-warm the Cartesia TTS connection and mask the agent-swap gap with a short filler/greeting break. Both are voice-pipeline behavior changes that **must be validated on real calls** — not committed blind.
- **#20 backend half:** optional worker→control-plane `agent.ready` event for a server-authoritative presence signal (the client-side timeout warning already ships here).

---

# Applied fixes — detail (branch `fix/sprint-2-defects` → PR #83)

Every row below is implemented, committed, and passing gates (backend `just check` 1031 passed; frontend `tsc`/`eslint`/`vitest` 199 passed).

| # | Defect | Fix applied | Key files | Commit(s) |
|---|---|---|---|---|
| 1 | Copy icon not working in invitation URL | Clipboard `execCommand` fallback now appends + focuses + selects its textarea **inside the active Radix dialog** (`[role="dialog"]`) instead of `document.body`, and returns `false` when `execCommand` fails so the UI can react. | `vera-frontend/src/lib/clipboard.ts` | `d82fd78` |
| 2 | "Action" column header misaligned | Deactivate button margin `-ml-2.5` → `-ml-2` to match the cell's `p-2` padding, so the label sits flush under the header. | `vera-frontend/src/pages/Users.tsx` | `cba25cf` |
| 3 | Duplicate eye icons in password field | Added `[&::-webkit-credentials-auto-fill-button]:hidden` + `[&::-webkit-strong-password-auto-fill-button]:hidden` alongside the existing Edge `::-ms-reveal` rule, hiding Chrome/Safari's native affordance. | `vera-frontend/src/components/ui/password-input.tsx` | `d82fd78` |
| 9 | Deactivate notice shows too long | Added a `×` dismiss button that clears the notice immediately (auto-dismiss timer kept). | `vera-frontend/src/pages/Users.tsx` | `cba25cf` |
| 11 | Account creatable with invalid email | Invite request `email: str` → `EmailStr` (RFC-validated, 422 on malformed); added `email-validator` dep; integration test for invalid + valid. | `vera-backend/.../api/v1/users.py`, control-plane `pyproject.toml`, `tests/integration/control_plane/test_admin.py` | `510c9f0` |
| 14 | Password page reappears via browser Back | Post-activation navigations use `navigate(loginHref, { replace: true })` so Back can't return to the (bfcache) form; `no-store` on the accept path (with #13). | `vera-frontend/src/pages/AcceptInvite.tsx` | `520f70c` |
| 16 | Platform operator redirected to tenant login after logout | Capture `logoutPlane` from `account_type` **before** the user is nulled (in `forceLogout` + `logoutThunk.fulfilled`); new `selectLogoutRedirectPath` returns `/platform/login` vs `/login`; used by Topbar, IdleManager, RequireAuth. | `vera-frontend/src/store/authSlice.ts`, `components/layout/Topbar.tsx`, `components/auth/IdleManager.tsx`, `RequireAuth.tsx` | `d23e48d` |
| 18 | Tenant Access duration appends input | Added `onFocus={(e) => e.target.select()}` to the duration input so typing replaces the default. | `vera-frontend/src/pages/TenantAccess.tsx` | `caa0ead` |
| 19 | Voice Lab provider list stale after toggle | Provider fetch extracted to a stable callback and refetched when IVR navigation turns on (fresh list when the picker becomes relevant). | `vera-frontend/src/pages/VoiceLab.tsx` | `caa0ead` |
| 22 | AI conversation shown under the caller | `TranscriptEvent` gains `source` ("bot"/"rep"); the Voice Lab panel labels by `source` (bot→Agent) with a `role` fallback — matching the monitoring panel. | `vera-frontend/src/lib/api/transcription.ts`, `pages/VoiceLab.tsx` | `96b421f` |
| 23 | Show date format in DOB/date fields | For date-type leaves the hint now prefers `validation.date_format` over `default`, so a `default="N/A"` field still shows the format. | `vera-frontend/src/components/ibv/FieldRenderer.tsx` | `96b421f` |
| 27 | Diagnostic Testing copay/coinsurance blank when gated off | Populated the `"plain"` flavor's `_INAPPLICABLE` map with `copay="$0"`, `coinsurance="0%"`, `prior_auth="N/A"` (`covered` omitted — the DSL validator rejects `inapplicable_value` without an ancestor gate); recompiled the schema. Affects Diagnostic Testing, General Coverage, and disease-only. | `vera-backend/.../forms/authoring.py`, compiled `data/form_schemas/*.json` | `b73beec` |
| 13 | Set-Password shown for a deactivated user | New token-scoped `GET .../auth/invitations/validate` endpoint returns `valid`/`invalid`/`deactivated` (enumeration-safe uniform "invalid", no PHI, `no-store`, token not consumed); `AcceptInvite.tsx` pre-flights on mount and shows a clear message instead of the form; `no-store` on the accept response. | `vera-backend/.../api/v1/auth.py`, `tests/.../test_admin.py`, `vera-frontend/src/lib/auth/api.ts`, `pages/AcceptInvite.tsx` | `2aebf19`, `460e5ce`, `5da0cb1`, `05d83a8`, `6b82367` |
| 17 | Sign-in with an expired/replayed OTP | Added `user_identity.totp_last_used_timestep` (idempotent migration); `verify()` is now a pure detector that returns the matched ±1-window step and rejects a step `<=` the last used one. The tenant path persists via ORM (under `SELECT FOR UPDATE` to close the concurrent-login race); the platform (NULL-tenant) path persists via a new SECURITY DEFINER function (RLS-bound role can't UPDATE NULL-tenant rows). Keeps ±30s drift tolerance — no `valid_window=0` regression. | `vera-backend/.../auth/mfa.py`, `api/v1/auth.py`, `api/v1/platform_auth.py`, `models/auth.py`, 2 migrations, `tests/unit/auth/test_mfa.py` | `85170f6`, `d7f0c7d`, `6564b6f`, `d2e50ab`, `356cde9` |
| 25/26 | Distinct prerequisite highlight; remove from Spouse Gender | New optional `prerequisite_fields` DSL key (round-trips, empty→omitted); catalog marks Appointment Date, Appointment Type, Callback Number; recompiled. FE adds a `"prerequisite"` usage category (amber, adjustable) that wins over "system", plus a legend entry. Spouse Gender stays `context`/green (unchanged role) — satisfying #26. | `vera-backend/.../forms/dsl.py`, `catalog/ibv_standard.py`, compiled schema; `vera-frontend/src/lib/ibv/schema.ts`, `types.ts`, `components/ibv/usageMeta.ts` | `c695a2f`, `f20d3c0`, `3c1f383`, `e329856` |
| 28 | Placeholder text instead of uploaded values | Intake upload now validates every produced path against the schema's leaf set (v2 only) and returns 422 with the offending paths, instead of silently storing dead paths that never render. | `vera-backend/.../forms/intake.py`, `api/v1/patient_forms.py`, `tests/.../test_patient_forms_intake.py` | `02f303b` |
| 20 | No response from IVR/AI in Voice Lab (client-side half) | New `hasAgentParticipant()` helper (uses LiveKit's authoritative `isAgent` kind) + `useAgentJoinTimeout()` hook; after 15s Connected with no agent, the panel shows a clear "agent hasn't connected — worker may not be running" alert that auto-clears if the agent joins late. Backend `agent.ready` event is a noted follow-up. | `vera-frontend/src/lib/voice-lab/agentPresence.ts` (+test), `pages/VoiceLab.tsx` | `4cb8c9e` |

**Regression caught & fixed during the work:** the first #17 attempt persisted the timestep by mutating the ORM identity inside `verify()`, which flushed through the RLS-bound session and broke **platform** MFA login (a NULL-tenant row can't satisfy the tenant RLS `WITH CHECK`). Fixed by making `verify()` a pure detector so each caller persists under its own privilege (`356cde9`).

---

# Master status index (all defects)

Legend: ✅ Fixed in PR #83 · ☑️ Already fixed on `dev` (verify QA build) · ✔️ Verified — no defect · 🏷️ By design / product decision · 🛠️ Ops/data (no code) · ⏸️ Deferred (needs decision or live-call validation)

| # | Defect | Status | Note |
|---|---|---|---|
| 1 | Copy icon not working in invitation URL | ✅ Fixed | dialog-scoped clipboard fallback |
| 2 | "Action" column header misaligned | ✅ Fixed | margin `-ml-2` |
| 3 | Duplicate eye icons in password field | ✅ Fixed | suppress webkit affordance |
| 4 | Agent profile shown instead of VA profile | ☑️ On `dev` | sidebar reads `/auth/me` |
| 5 | Data Management not accessible after VA login | 🏷️ By design | VA holds only `voice_lab:sandbox`; grant `forms:read` needs product sign-off (Voice Lab nav already fixed on `dev`) |
| 6 | Generic error for deactivated VA login | ☑️ On `dev` | 403 + FE error serialization |
| 7 | AI ignores interruption, repeats question | ⏸️ Deferred | config change documented; validate on real calls |
| 8 | AI response delayed after transfer to human | ⏸️ Deferred | TTS pre-warm/handoff; validate on real calls |
| 9 | "Deactivate msg showing for a long time" | ✅ Fixed | dismiss button |
| 10 | AI fails to detect incorrect Member ID from IVR | ⏸️ Deferred (L) | PHI-wall gap; two designs written up, needs sign-off |
| 11 | Account creatable with invalid email | ✅ Fixed | `EmailStr` |
| 12 | Invite link usable after activation | ✔️ No defect | prod uses single-use `RedisInvitationStore` |
| 13 | Set-Password page shown for deactivated user | ✅ Fixed | `validate` endpoint + FE pre-flight |
| 14 | Password page reappears via browser Back | ✅ Fixed | `navigate(replace:true)` + no-store |
| 15 | Session-timeout warning popup | ☑️ On `dev` | countdown dialog driven by `/auth/me` |
| 16 | Platform operator → tenant login after logout | ✅ Fixed | `logoutPlane` redirect |
| 17 | Sign-in with expired/replayed OTP | ✅ Fixed | single-use TOTP timestep |
| 18 | Tenant Access duration appends input | ✅ Fixed | select-on-focus |
| 19 | Voice Lab provider details not refreshed | ✅ Fixed | refetch on IVR toggle |
| 20 | No response from IVR/AI in Voice Lab | ✅ Fixed (client half) | 15s "agent hasn't joined" warning; backend `agent.ready` event = follow-up |
| 21 | Deactivate provider → deactivate its playbook? | 🏷️ Not a bug | dispatch join already excludes inactive-provider playbooks |
| 22 | AI conversation displayed under the caller | ✅ Fixed | attribute by `source` |
| 23 | Show date format in DOB/date field | ✅ Fixed | prefer `date_format` over default |
| 24 | Patient Gender field displays as date | 🛠️ Ops/data | stale `schema_version`; `just compile-schemas && seed-schemas` |
| 25 | Highlight prerequisite fields distinctly | ✅ Fixed | `prerequisite_fields` DSL key + amber |
| 26 | Remove prerequisite highlight from Spouse Gender | ✅ Fixed | stays context/green (excluded from prerequisite) |
| 27 | Diagnostic Testing copay/coinsurance blank | ✅ Fixed | plain-flavor reference values |
| 28 | Placeholder text instead of uploaded values | ✅ Fixed | intake 422 on unknown paths |

**Totals:** ✅ 18 fixed · ☑️ 3 already on `dev` · ✔️ 1 no-defect · 🏷️ 2 by-design · 🛠️ 1 ops · ⏸️ 3 deferred (#7, #8, #10).
