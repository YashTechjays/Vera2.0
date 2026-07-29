# Remediation: defects found by the call-flow eval harness

**Date:** 2026-07-30
**Source:** one full 3-scenario run of `apps/agent_worker/tests/evals` (11m05s)

| scenario | plan | answers extracted |
| --- | --- | --- |
| `cooperative rep` | 7 tasks / 182 fields | 44 |
| `mandate says covered, rep says not covered` | focused, 3 tasks / 4 fields | 3 |
| `policy is not active` | focused, 2 tasks / 3 fields | 2 |

The evaluator LLM reported 11 `FAIL` lines. They collapse into **6 product defects and 2 harness
defects**, and one product defect causes several of the others.

None of the six were caught by the existing unit tests, and all six are visible in a single run.

---

## What this run proved works (first evidence for either)

- **Contradiction rule, end to end.** `contradictions pass [63]`. VERA spoke the schema's `clarify`
  string verbatim — *"Earlier you mentioned there is an infertility plan mandate on this policy, but
  infertility treatment is showing as not covered…"* — then accepted the rep's answer. Rep turn →
  extraction → `RuleEngine.evaluate` → `ReAsk` → `apply_directive_now` all fired.
- **Flow rule, end to end.** `flow_rules pass [41]`. The rep reported an inactive policy and the call
  short-circuited to `wrap_up` rather than walking the remaining tasks.
- **IVR navigation: `pass` in all three scenarios.** Never handed off to the automated assistant.

---

# Product defects

All six are in `apps/agent_worker`, so they belong on worker branches — not on
`feat/call-flow-eval-harness`.

## P1 — `task_complete` / `gap_complete` fires in the same turn as the final question

**Severity: highest — this one loses data, not just polish.**

Observed in every scenario:

```
VERA : Is infertility treatment covered under this plan?
TOOL : task_complete
>>>> HANDOFF PlanTaskAgent -> PlanTaskAgent
REP  : Yes, infertility treatment is covered
```

VERA asks and completes in the same turn, so the rep's answer arrives while the **next** task is
active. `TaskObserver` is whitelisted to the active task's field paths (`observer.py`, `_rotate`),
so the answer is **unextractable and silently dropped**.

**It cascades.** Dropped answers never reach `_answers`, so `gap_fields()` still considers those
fields open and the gap pass re-asks them. That produced `answer_handling FAIL [133]` ("re-asked
several questions during the gap pass that the representative had already answered") and much of
`task_handoffs FAIL`. Fixing P1 should retire three FAIL lines.

**Fix options, most to least preferred**

1. **Attribute a rep turn to the task that asked the question** — carry the asking task's index with
   the turn so a late answer is extracted against the correct whitelist. Fixes the data loss wherever
   it occurs, not just at handoffs.
2. **Defer the swap by one turn** — `task_complete` records intent; the agent swap happens after the
   next rep turn has been ingested.
3. **Prompt-only** — forbid `task_complete` in a turn that also asks a question. Cheapest, least
   reliable, and does not fix the underlying attribution.

**Verification:** `extracted()` on the cooperative-rep scenario should rise materially from 44, and
the gap pass should stop re-asking already-answered fields.

## P2 — the call signs off two or three times

`closing FAIL` in all three scenarios. Scenario 3 signed off **three times**:

```
VERA : That's everything I need today. Thank you so much for all your help — have a wonderful day!
VERA : Thank you so much for your help, Martha. Have a wonderful day. Goodbye.
VERA : I hope you have a great day!
```

**Cause:** the closing task's `outro` is spoken via `session.say` while `WrapUpAgent.on_enter`
separately generates its own goodbye. Both reach the rep.

**Fix:** one closer, not both — suppress the closing task's outro when wrap-up will speak, or keep
wrap-up silent when the closer already signed off.

Currently tracked as an `xfail` in the harness; this run shows it is sometimes triple, not double.

## P3 — a task outro lands inside the gap pass, making a mid-call moment sound final

`gap_conduct FAIL [78]` — *"claimed everything on her list was covered mid-call"*:

```
VERA : Thanks so much for your patience — that covers everything on my list.   <- task outro
VERA : To confirm, I have your name down as Martha Reed…                        <- still mid-call
```

`_gap_block` explicitly forbids the gap agent from claiming completeness, **and it obeyed** — that
phrase is a task `outro` spoken via `session.say`, not text the gap agent generated. The guardrail is
being bypassed by speech the gap agent never produced.

**Fix:** suppress the outro on the transition that diverts into the gap pass
(`_maybe_enter_gap_pass`), or speak it after the sweep completes.

## P4 — a task whose fields are all gated out still announces and closes itself

```
VERA : Now I'd like to ask about male partner fertility coverage.
VERA : Thanks, that covers the male partner benefits. Just a moment.
```

**VERA was correct to ask nothing.** Verified against the compiled plan:
`male_partner.applicable_when` is `None`, so the task is always entered, but its first field
(`sections.male_partner_coverage.male_partner_covered`) is gated on shared condition
`male_partner_in_scope`, which evaluates `False` for this case — so all 9 fields are inapplicable.

The logic is right; the conduct is not. Announcing a section and closing it in the same breath sounds
broken to a representative.

**Fix:** when a task has no applicable, unanswered fields on entry, skip it silently — the same shape
as `GapTaskAgent.on_enter`'s `if not fields:` branch.

**Interaction:** coordinate with the handoff-context window on `feat/handoff-context-window` — a
silently-skipped task must be *transparent* for context carry, or it will hand its successor an empty
window (the same class of bug already fixed there for silent gap agents).

## P5 — several questions bundled into one turn

`question_coverage FAIL [45]`, and plainly in the transcript:

> *"Is this plan self insured or fully funded? And is the employer group supporting this plan a small
> group or a large group?"*

The compiled ground rules say one question at a time. On a live call a rep typically answers one half
and the other is lost.

**Fix:** strengthen the per-turn instruction. Consider flagging assistant turns containing multiple
question marks so the harness can measure this **deterministically** rather than relying on the
judge.

## P6 — duplicate consecutive `press_keypad` calls

Two consecutive presses in scenario 1, **four** in scenario 2, two in scenario 3. Scenario 3 also
spoke a bare `*` as text.

On a live call duplicate DTMF can misnavigate a menu (pressing `2` twice may land elsewhere).

**Severity unverified:** text mode mocks the tone transport, so the repeated *calls* are real but
their consequence on a live line is not established. Size this on a real call before treating it as
urgent.

**Fix:** investigate whether the model is retrying because it misreads the tool result as a failure;
consider making `press_keypad` idempotent within a single menu prompt.

---

# Harness / evaluator defects

## H1 — the evaluator has no applicability context, so it fails correct behaviour — FIXED 2026-07-30

`question_coverage FAIL [118]` and part of `task_handoffs FAIL [118]` are **wrong**. The evaluator
reported the male-partner section as "skipped without asking any questions" when skipping was correct
(see P4) — it does not know that gates exist.

**This is the most important harness fix.** An evaluator that fails correct behaviour trains people
to ignore it, which is worse than having no evaluator.

**Fix:** include per-field applicability in the evaluator's brief — render each task's questions with
the ones gated out for this call marked as such, and state that a gated-out question **must not** be
asked. `is_applicable(field.gates, answers, shared)` already exists in
`packages/vera_core/src/vera_core/forms/conditions.py`.

**Done.** `render_tasks(plan, answers)` now marks each excluded question
`[GATED OUT — must NOT be asked on this call]` and heads a fully-gated task with "completing this
task without asking anything is CORRECT". The prompt explains the marker, and the
`question_coverage` / `task_handoffs` dimensions were reworded to say APPLICABLE. `render_rules`
also now emits `note` and `clarify`, so the judge compares push-back against the schema's own
wording instead of a standard it invented. Both helpers moved from `conftest.py` into `judge.py`
(brief-building belongs with the judge) and are covered by 5 new LLM-free tests that run in
`just check`.

Verified on a re-run of the cooperative-rep scenario:
`question_coverage FAIL "entire Male Partner Coverage section skipped"` became
`question_coverage pass "All applicable questions from the task list were successfully covered."`
The genuine P1/P2 failures remained, which is the point — the false positive went and the real
findings stayed.

## H2 — the scenario banner claims `full_walk=True` on a focused plan

`===== mandate says covered…: 3 tasks, 4 fields, full_walk=True =====` is misleading: a scenario's
`focus_fields` overrides the flag inside `load_published_plan`, so the plan *was* narrowed. Print
`focused` when a scenario narrows the plan.

---

# Sequencing

1. **H1 first.** Until the evaluator stops failing correct behaviour its verdicts cannot be trusted
   to drive product work — and it is a small change to the brief.
2. **P1 next.** The only defect that loses data, and it retires the P1-caused `answer_handling` and
   `task_handoffs` failures at the same time.
3. **P2 and P3.** Both small, both damage the call's final impression.
4. **P4**, coordinated with the window work (see the interaction note).
5. **P5, P6, H2.** Real but lower stakes; P6 needs a live call to size.

Re-run the 3-scenario suite after each change and diff the scorecards. A fix is done when its FAIL
line is gone **and** the deterministic assertions still pass.

---

# Caveats on this evidence

- Every item is grounded in the run's transcript. P4 and H1 were additionally verified against the
  compiled plan rather than inferred from the conversation.
- **P6's severity is unverified** — text mode mocks DTMF.
- **Extraction is nondeterministic.** Scorecards vary between runs (an earlier run failed
  `scope_discipline` and passed `contradictions` differently). A single clean run is not proof a
  defect is fixed; re-run before concluding.
- The harness cannot see STT damage, latency, or a rule that fired too late to matter — it settles
  extraction between turns, so rules fire earlier and more reliably than on a real call.
