# Call transcript analysis — first real plan-runtime call (2026-07-14)

Analysis of the UnitedHealthcare verification call (Morgan Reyes form) against the
**actual fused CallPlan** for that form (schema + published prompt version + intake
prefill, dumped task-by-task offline). Scope: mistakes of the bot under the
`feat/voice-agent-runtime-v3` plan-only runtime, with root causes mapped to the
branch's known gaps and a prioritized remediation plan. **Status: analysis only —
no fixes implemented yet.**

---

## 1. What worked (evidence the branch functions end-to-end)

- **IVR navigation**: menu traversal, `press 2` keypad (DTMF), member ID + DOB
  spoken from `agent_context` (dev PR #81), survey/loop escapes, human reached.
- **Handoff**: `transfer_to_verification` → first PlanTaskAgent; patient IVR turn
  config reverted to the snappy default.
- **Hydration**: intro spoke real values ("Morgan Reyes", DOB, Demo Health
  Partners, Dr. Jane Smith) from intake prefill.
- **Task chain**: per-task intros/outros ("Great, let me pull up my questions...",
  "Perfect, that covers the plan basics...") through all 7 tasks to wrap-up.
- **coverage task — gating honored perfectly**: infertility covered = No → all
  ~40 per-service questions (IUI/IVF/cryo CPTs, gated ×1-2 on coverage) correctly
  skipped.
- **closing_admin — near-perfect**: every gate honored (TPA No → skip name;
  PBM Yes → name + phone; ISP No → skip name/phone; enrollment Yes → provider
  name + phone).
- **wrap_up**: representative name collected (+ spelling); `<spell>` markup used
  for IDs; deductible/OOP arithmetic double-checked aloud (500 remaining, 1300
  remaining — both confirmed).

---

## 2. Mistakes (ranked)

### M1 — CRITICAL: ~11 of 18 `insurance_basics` questions never asked, then `task_complete`

Asked only: plan_type, cob_status, policy_number (as an open ask — see M3),
group_name, group_number.

**Missed** (all ask-role fields of the task):

| Field | Note |
|---|---|
| `insurance_information.doctor_inside_network` | user-noticed |
| `insurance_information.facility_inside_network` | user-noticed |
| `insurance_information.out_of_network_coverage` | user-noticed (gated ×1) |
| `insurance_information.policy_situs` | |
| `benefit_coverage.benefit_year_type` | |
| `benefit_coverage.plan_effective_date` | |
| `benefit_coverage.plan_year_information` | |
| `benefit_coverage.coverage_type` | gates family deductible/OOP questions |
| `benefit_coverage.pcp_referral_required` | gated ×1 |
| `benefit_coverage.telehealth_covered` | |
| `benefit_coverage.plan_fund_type` | contradiction input |
| `benefit_coverage.employer_support_size` | contradiction input |
| `benefit_coverage.infertility_plan_mandate` | contradiction input |

**Cascade damage**: the `no_out_of_network_coverage` TERMINATION rule (requires
doctor+facility+OON all No) and the `mandate_requires_infertility_coverage`
contradiction could never fire — their inputs were never collected. The family
deductible/OOP gates (on coverage_type) went indeterminate, so those questions were
silently skipped too.

**Root causes**: (a) `task_complete` is pure LLM judgment — nothing deterministic
tracks which of the task's fields have answers (the Phase-2 Observer is not built);
(b) a 20-question task prompt invites LLM attention drift — it completed after the
"plan basics" cluster it was mentally on.

### M2 — CRITICAL: conversation memory is wiped at every handoff

Verified in the installed livekit-agents source: a tool-returned agent keeps the
`chat_ctx` it was **constructed** with — `ChatContext.empty()` for our pre-built
agents (`voice/agent.py:78`); the automatic history merge applies only to inline
AgentTasks (`voice/agent.py:951`). The documented handoff pattern is passing prior
history explicitly.

**Transcript symptoms**: the task-2 agent RE-INTRODUCED itself ("Hello, my name is
VERA. I'm calling on behalf of Demo Health Partners…") violating the "never
re-introduce yourself" ground rule, and RE-ASKED the member ID that Vera itself had
already spoken in the IVR phase (POL-661522).

**Fix (small, no Phase 2 needed)**: in `task_complete`/`advance_from`, copy the
outgoing agent's `chat_ctx` (excluding instructions + function-call items, mirroring
the library's own merge flags) into the successor via
`await successor.update_chat_ctx(...)`; same at the IVR → plan handoff in
`transfer_to_verification`.

### M3 — HIGH: confirm-role prefill unusable → conflicting member ID collected

`policy_number` is a **confirm-role** leaf: *"I have the member ID as `{{value}}` —
can you confirm that is correct?"*. But `{{value}}` is a runtime sentinel kept
verbatim by the fuse, and confirm-role leaves are **excluded** from the
Known-information block — so the agent had no value to confirm against. It degraded
to an open ask ("Can I start by getting the subscriber ID, please?") and accepted
**A12345678**, while intake/agent_context carried **POL-661522**. The call proceeded
on a conflicting member ID with zero mismatch detection.

**Fix**: expose prefilled confirm-role values to the agent — extend
`PrefillFuser.fuse` with a "values already on file" block (confirm-role leaves with
a prefill), rendered into agent instructions, so the confirm prompt can actually be
executed and a rep correction surfaces as an explicit mismatch.

### M4 — HIGH: no sanity/contradiction enforcement

Accepted lifetime maximum **total = 200, met = 1, remaining = 300** — remaining >
total — without pushback. Two gaps: the Phase-2 rule engine doesn't exist (schema
contradictions are prompt-text-only), and the schema itself has no
numeric-consistency contradiction for total/met/remaining triplets.

### M5 — MEDIUM (known Phase-2 gap): rules and mid-call gating are advisory only

Even with the right answers collected, nothing at runtime terminates / skips /
re-asks (`apply_directive` is a stub); the controller's answers snapshot is seeded
from intake prefill and never updated mid-call (`update_answers` has no caller), so
task-level `applicable_when` cannot react to answers learned during the call.

### M6 — LOW: cosmetics / data

- **"Dr. Dr. Jane Smith"** — template says `Dr. {{doctor_name}}` and the intake
  value already contains "Dr." → honorific duplication (dedup at fuse, or data
  hygiene).
- **DOB spoken as raw ISO** "1991-04-12" — `_render_value` renders dates verbatim;
  should render TTS-friendly ("April 12, 1991").
- Split turn "Thanks so much for your / time" (interruption artifact).
- IVR "would you like to hear those details again" repeat loop (navigator prompt
  tuning opportunity).
- Reference number was volunteered by the rep and captured, but never explicitly
  confirmed.

---

## 3. Remediation plan (prioritized; user-approved order = quick wins first)

### Fix 1 (M2) — carry chat context across handoffs — SMALL
`plan_runtime.py` `_task_complete`/`advance_from` + `ivr_agent.py`
`transfer_to_verification`: copy prior `chat_ctx` (minus instructions/function
calls) into the successor before returning it. Shared helper in `agent.py`. TDD:
successor ctx contains prior turns; excludes old instructions.

### Fix 2 (M3) — expose confirm-role prefills — SMALL
`call_plan.py` `PrefillFuser.fuse`: build a "values already on file" block from
confirm-role leaves with prefilled values; render it in
`plan_runtime._instructions`. TDD: policy_number appears there; context leaves stay
in Known information; input-role stays excluded.

### Fix 3 (M6) — cosmetics — SMALL
`call_plan.py`: honorific dedup after hydration ("Dr. Dr." → "Dr."); `_render_value`
formats ISO dates for speech. TDD both.

### Fix 4 (M1 interim) — question-coverage discipline — MEDIUM (optional stopgap)
Number the questions in each compiled task prompt and state the expected count in
the `task_complete` tool description. Discourages premature completion but does not
guarantee coverage — the guarantee requires the Observer.

### Fix 5 (M1/M4/M5 systemic) — Phase 2: Observer + rule engine — LARGE
Already designed (plan §2.1–2.5): in-worker Observer extracts answers from the live
transcript → `PlanRunState.answers` + `field_answer` (source=`ai_call`) via the
worker-events bridge; rule engine evaluates flow_rules/contradictions live →
terminate / skip / re-ask via `apply_directive`; `update_answers` keeps mid-call
gating truthful. Plus: author numeric-sanity contradictions (total/met/remaining)
in the schema so M4 has a rule to fire.

### Verification for Fixes 1–3
`just check` green (known phi_codec Gemini flake excepted) → re-dispatch the Morgan
Reyes form → confirm: task-2 agent does NOT re-introduce itself; the member-ID
exchange OPENS as a confirmation of POL-661522; the intro speaks "April 12, 1991"
and a single "Dr.".
