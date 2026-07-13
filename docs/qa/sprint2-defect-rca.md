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
