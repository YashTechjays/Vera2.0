# Remediation: defects found by the call-flow eval harness

**Date:** 2026-07-30
**Source:** two full 3-scenario runs of `apps/agent_worker/tests/evals` (11m05s, then 13m08s
after the H1 fix)

| scenario | plan | answers extracted (run 1 → 2 → 3) |
| --- | --- | --- |
| `cooperative rep` | 7 tasks / 182 fields | 44 → 50 → 45 |
| `mandate says covered, rep says not covered` | focused, 3 tasks / 4 fields | 3 → 2 → 3 |
| `policy is not active` | focused, 2 tasks / 3 fields | 2 → **0** → **1** |

**Run 3 was the first with all four harness defects fixed, and the fact block immediately earned
its place** — it turned two false passes into correct failures:

- `contradictions FAIL` — *"a contradiction occurred, but according to the facts the rule did not
  fire"*. Runs 1–2 scored this `pass` because VERA spoke the right words. She was reading them from
  her prompt, not from a directive (see the correction below).
- `flow_rules FAIL` — *"should have fired when the representative confirmed the policy was inactive,
  but the facts indicate no rule fired"*.

Both are real: the deterministic tests skipped with `mandate='No', covered='No'` and
`is_insurance_active=None`, so no directive existed either time.

> **Run 2 changed the picture in three ways.** H1's fix worked (the false coverage failure is
> gone). It surfaced a **new production defect** — P7, `focus_call_plan` not narrowing the spoken
> question list. And it exposed a **false PASS** (H4), which is more dangerous than the false fail
> H1 caused. See "Run 2 findings" below.

The evaluator LLM reported 11 `FAIL` lines. They collapse into **6 product defects and 2 harness
defects**, and one product defect causes several of the others.

None of the six were caught by the existing unit tests, and all six are visible in a single run.

---

## What this run proved works (first evidence for either)

- **Contradiction rule, end to end.** Confirmed in runs 1–2 by the deterministic assertion
  `expect_rule in fired_rules()` passing (not skipping), i.e. a directive really did reach
  `apply_directive_now`.

  > **Correction (run 3).** An earlier version of this note cited VERA speaking the schema's
  > `clarify` string verbatim as the evidence. **That was the wrong evidence.**
  > `forms/prompting.py:433-442` renders every contradiction's `clarify` text *into the compiled task
  > prompt* — `f'If {cond}: {reason} Push back once, saying: "{contra.clarify}"'` — so VERA has that
  > wording in her instructions and produces it whether or not the rule engine fires.
  >
  > Run 3 proves it: the mandate contradiction push-back was spoken **verbatim** while
  > `fired_rules()` was empty and extraction had the mandate wrong (P9).
  >
  > Two consequences. **A transcript can never show whether a contradiction rule fired** — the
  > wording is identical either way, which is precisely why H4's fact block is load-bearing rather
  > than a nicety. And the prompt acts as defence-in-depth: the rep still gets a correct push-back
  > when the rule engine is dead, so a broken rule engine is invisible in conduct. Good for the call,
  > bad for detection. Worth deciding deliberately whether the prompt should keep that duty, since
  > the `ReAsk` directive's added value is that it *interrupts* and forces the re-ask, where the
  > prompt is only best-effort.
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

**Reclassified in run 3: this is P10, not a DTMF-specific quirk.** The same duplicate-invocation
pattern hits `task_complete` and `gap_complete` too, where it costs a whole task. Run 3 showed
`press_keypad` three times in one turn. Fix it as part of P10 — per-turn idempotence on the tool —
rather than as a keypad special case. Its live severity still needs a real call to size, because
text mode mocks the tone transport.

## P9 — extraction recorded a WRONG value, not just a missing one

**New in run 3. Severity: alongside P1 — this one corrupts data rather than losing it.**

Scenario 2, verbatim:

```
VERA : Is there an infertility plan mandate on this policy?
REP  : Yes, there is an infertility plan mandate on this policy.
```

The harness then skipped its rule test with:

```
trigger not extracted: mandate='No', covered='No'
```

`infertility_plan_mandate` was recorded as **`No`** when the representative plainly said **Yes**.
That is worse than the answer being dropped (P1):

- the contradiction rule evaluates `mandate == "Yes" AND covered == "No"`, so it silently never
  fires — the call looks fine;
- `gap_fields()` treats the field as answered, so the gap pass will not re-ask it;
- the wrong value flows to `field_answer` and into the form.

A dropped answer eventually shows up as an unanswered field. A **wrong** one shows up as a confident
lie, and nothing in the pipeline flags it.

**Fix:** investigate `ResilientAnswerExtractor` on this turn — whether the whitelist/prompt is
inverting a yes/no, or whether the window handed it the wrong turn (P1's attribution bug could
supply a neighbouring answer). Start by logging the extractor's raw reply for this exact exchange.

**Verification:** a deterministic extractor test on this transcript pair asserting
`infertility_plan_mandate == "Yes"`. This one deserves a regression test, not just a re-run.

## P10 — a tool is invoked twice in one turn, advancing the chain twice

**New in run 3. Probably the mechanism behind P6 and P8, so fixing it may retire both.**

Run 3 shows the same tool emitted repeatedly within a single turn, and **each call advances the
chain**:

```
TOOL : task_complete
TOOL : task_complete
>>>> HANDOFF PlanTaskAgent -> PlanTaskAgent
>>>> HANDOFF PlanTaskAgent -> PlanTaskAgent      <- two tasks traversed, one of them never asked
```

Also seen as `gap_complete` twice (two gap handoffs in one turn) and `press_keypad` three times in
the IVR phase.

**This explains the skipped questions.** Scenario 1 reported `task_handoffs FAIL [60]`
("transitioned from insurance_basics to coverage without asking all applicable questions") and
`question_coverage FAIL [58]` (Plan Effective Date and Plan Year Information never asked). A double
`task_complete` walks straight through a task without entering its question loop — which is exactly
what P8 describes.

It also reframes **P6**: duplicate `press_keypad` is not a DTMF-specific quirk but the same
duplicate-invocation pattern, so its live severity matters more than first assumed.

**Fix:** make chain-advancing tools idempotent per turn — a second `task_complete` / `gap_complete`
in the same turn should be a no-op returning a plain string, not a second `Agent`. The guard belongs
in `PlanTaskAgent._task_complete` / `GapTaskAgent._gap_complete`, next to the existing
`takeover_engaged` early-return which already uses that shape.

**Verification:** a unit test invoking `task_complete` twice without an intervening turn must
produce one handoff. Then re-run and confirm no doubled `HANDOFF` lines and no skipped tasks.

## P7 — `focus_call_plan` narrows tracked fields but NOT the spoken question list

**New in run 2. Production defect on the FOCUSED-retry path, found via the harness.**

`focus_call_plan` (`packages/vera_core/src/vera_core/forms/call_plan.py:190`) does only:

```python
task.model_copy(update={"fields": kept})
```

It never touches `task.prompt`. The compiled prompt text still enumerates **every** question in the
task, so narrowing changes what the plan *tracks* but not what VERA *asks*.

Its own docstring says the opposite:

> *"The agent then asks ONLY the still-missing data points — with no announcement that this is a
> retry."*

**Evidence** (run 2, scenario 2, focused to 4 fields): VERA asked network status for the doctor and
facility, plan type, coordination of benefits, member ID, group name and number, contract state,
benefit year, individual-vs-family, telehealth, self-insured-vs-fully-funded, employer group size,
the mandate, and four CPT codes.

**Why it matters beyond the harness:** `focus_call_plan` is the production path for a FOCUSED retry.
If the prompt is not narrowed, a retry call re-asks the whole task instead of just the missing
fields — the exact behaviour the docstring says it must avoid, and a needlessly long call for the
payer rep.

**Fix:** narrow the task prompt alongside the fields — either re-render the prompt from the kept
fields, or append an explicit instruction naming the only questions still in scope. Needs a decision
from whoever owns the retry design; the prompt compiler is in `forms/prompting.py`.

**Verification:** a focused scenario should show VERA asking only the kept fields' questions. That
also removes the false `scope_discipline` failure described in H3.

## P8 — an applicable required question can go unasked

**New in run 2.** `question_coverage FAIL` on the cooperative-rep scenario:

> *"VERA failed to ask the applicable 'Plan Year Information' question from the insurance_basics
> task."*

This is a **genuine** coverage miss, and it is only visible now that H1 taught the evaluator to
distinguish applicable from gated-out questions. Run 1's coverage failure was noise; this one is
signal.

**Mechanism identified in run 3: this is P10.** A doubled `task_complete` in one turn produces two
handoffs, walking straight past a task without entering its question loop. Run 3 named a second
missed field (Plan Effective Date) and reported `task_handoffs FAIL [60]` — *"transitioned from
insurance_basics to coverage without asking all applicable questions"* — on a turn that shows two
`task_complete` calls. Fix P10 and re-check before treating P8 as separate work.

---

# Run 2 findings: what the H1 fix changed

**H1's fix worked.** Same scenario, before and after:

```
run 1:  question_coverage  FAIL  entire Male Partner Coverage section skipped
run 2:  question_coverage  pass  All applicable questions ... successfully covered   (scenario 2)
run 2:  question_coverage  FAIL  'Plan Year Information' not asked                   (scenario 1)
```

The false failure is gone, and the dimension now produces a specific, actionable finding instead
(P8). The word "applicable" appears in the reasons, so the gate context is being used.

**The contradiction rule fired again — two runs in a row**, with the schema's `clarify` wording
verbatim, and `contradictions pass` both times. That is now a reliably reproducing capability
rather than a one-off.

**P2 is worse than recorded.** Run 2 produced **three** sign-offs in two separate scenarios,
including `"The call has ended."` spoken aloud to the rep as a third farewell.

**P1 is confirmed harder.** Run 2's `tool_calls FAIL [5]` names it directly: *"called press_keypad
four times in a row and called task_complete twice consecutively."* Scenario 3 shows
`task_complete` and `gap_complete` in a single turn producing two handoffs at once.

**Extraction is alarmingly variable.** 44→50, 3→2, and **2→0**. A scenario extracting *zero*
answers means the Observer contributed nothing at all, so no rule could fire and the gap pass had
no state — yet the scorecard still read mostly `pass`. See H4.

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

## H4 — the evaluator PASSED a flow rule that provably never fired — FIXED 2026-07-30

**New in run 2. Highest-priority harness defect — a false pass hides a defect, which is worse than
the false fail H1 caused.**

Scenario 3 scored:

> `flow_rules  pass  [39]  VERA correctly triggered the insurance_not_active flow rule and skipped
> to the wrap-up task.`

**It cannot have.** That same run reported `0 answers extracted`. With nothing in `_answers`,
`is_insurance_active` was never set, so `RuleEngine.evaluate` could not have returned a directive.
The harness also prints `directives fired: [...]` whenever one reaches the controller, and that line
is absent.

The judge inferred "the rule fired" from the call ending early — but the scenario's plan is only two
tasks, so it would end early regardless. **A transcript cannot show whether a directive fired.**

This corrects a design decision recorded when the evaluator was built: the judge was deliberately
given *no* structural facts, on the grounds that it should grade conduct rather than re-derive things
we already know. That reasoning holds for handoffs and tool calls, which are visible in the
transcript. It is **wrong for rule firing**, which is invisible.

**Fix:** feed the recorded facts into the brief for the rule dimensions specifically —
`CallRun.fired_rules()` (already collected via the `apply_directive_now` recorder) and the extracted
answer count. State that a rule listed as fired **did** fire, one absent did **not**, and that the
judge must grade whether the call *responded correctly*, not whether it fired.

**Verification:** re-run scenario 3. With `fired_rules()` empty it must report `flow_rules fail`
(the rep said the policy was inactive and no rule fired), not `pass`.

**Fix applied 2026-07-30. Rendering verified; judge obedience NOT yet proven — see below.**
`render_facts(fired_rules, answers_extracted, focused=…)` adds a
`# Recorded facts about this run (ground truth)` block at the head of the brief:
the authoritative fired-rule list (or "NO rule fired"), the extracted-answer count with the note
that 0 makes firing impossible, and the focused disclaimer for H3. The prompt now states the block
is instrumentation rather than inference and that **whether a rule fired is invisible in a
transcript**, so the judge must never conclude firing from the call changing course. The
`flow_rules` and `contradictions` dimensions were rewritten to compare facts against transcript —
condition met but nothing fired is now explicitly a FAIL. Six LLM-free tests cover the block.

This reverses the "no structural facts" decision taken when the evaluator was built. That decision
was right for handoffs and tool calls, which the transcript shows; it was wrong for rule firing,
which it cannot.

**Proven closed.** A live scenario re-run could not settle this: scenario 3 came back
`flow_rules pass — "the insurance_not_active rule correctly fired"`, which was **correct** that time
(`test_inactive_policy_short_circuits_the_call` passed rather than skipped, so `fired_rules()` really
did contain the rule). The failure condition never recurred, so the run exercised the fact block
without testing obedience.

So the contradiction was constructed instead: a transcript reading as a textbook short-circuit,
paired with facts saying nothing fired. Result:

```
flow_rules  FAIL  The representative confirmed the insurance was not active, but the
                  corresponding flow rule failed to fire.
```

The judge trusted the fact block over the conversation — exactly the inference it used to make and
no longer does.

That check is now a permanent regression guard, `test_judge_obedience.py`, rather than a throwaway
probe: one LLM call, `evals`-marked so `just check` never collects it, and it covers a property no
scenario run can reach. Six further LLM-free tests pin the rendering itself.

## H3 — focused scenarios produce a false `scope_discipline` failure

**New in run 2.** Scenario 2 scored:

> `scope_discipline  FAIL  [36]  VERA asked several off-script questions not present in the compiled
> task list, including network status, telehealth coverage, and specific CPT codes.`

VERA was following the task prompt, which still contains every question (P7). The judge was shown
only the *narrowed* field list, so legitimate questions looked invented.

The root cause is P7 and fixing it removes this. Until then, either also show the judge each task's
compiled `prompt`, or skip `scope_discipline` on scenarios that narrow the plan — do **not** leave it
reporting a fault that is not one.

**Fix applied 2026-07-30 — awaiting a live re-run.** A focused scenario's fact block now says the
plan was narrowed, that narrowing does **not** shorten the spoken question list, and that questions
outside the task list are therefore not off-script — so `scope_discipline` must come back `n/a`.
This is a disclosure, not a repair: it stops the false failure while P7 remains open, and should be
removed once P7 is fixed so the dimension becomes meaningful again on focused runs.

## H2 — the scenario banner claims `full_walk=True` on a focused plan — FIXED 2026-07-30

`===== mandate says covered…: 3 tasks, 4 fields, full_walk=True =====` is misleading: a scenario's
`focus_fields` overrides the flag inside `load_published_plan`, so the plan *was* narrowed. Print
`focused` when a scenario narrows the plan.

**Fixed.** The banner now reports the plan's real mode — `focused` whenever the scenario sets
`focus_fields`, `full walk` only when the plan genuinely was not narrowed. The banner is the first
thing a reader trusts, so it should not be the first thing they have to distrust.

---

# Sequencing

Revised after run 2. **All four harness defects are now closed, so the evaluator's verdicts can be
trusted to drive the product work below.**

1. ~~**H1, H2, H3, H4**~~ — **all done 2026-07-30.** H3 is a disclosure rather than a repair: remove
   it when P7 lands so `scope_discipline` becomes meaningful on focused runs again.
2. **P10 next.** Cheapest high-value fix: per-turn idempotence on `task_complete` / `gap_complete` /
   `press_keypad`, in the same early-return shape `takeover_engaged` already uses. It is the
   mechanism behind **P8** and reclassifies **P6**, so one small change should retire three items.
3. **P1.** Loses data. Retires the P1-caused `answer_handling` / `task_handoffs` failures.
4. **P9.** Corrupts data, and is the harder of the two extraction defects to detect — a wrong value
   never surfaces as a gap. Needs the extractor's raw reply logged on the failing exchange first.
5. **P7.** A production defect on the retry path; fixing it also lets H3's disclosure be removed.
   Needs a decision on how the prompt gets narrowed.
6. **P2 and P3.** Both small, both damage the call's final impression. P2 is consistently **triple**
   across runs — run 3's third farewell was `"I hope you have a great day!"`.
7. **P4**, coordinated with the window work (see the interaction note).
8. **P5.** Lower stakes; still visible in run 3 as two-questions-per-turn.

**Decide, don't drift:** should the compiled prompt keep carrying contradiction `clarify` text now
that the rule engine also handles it? Today the prompt masks rule-engine failures (see the
correction above). Either is defensible — but it should be a choice, not an accident.

**Worth its own investigation:** extraction fell 2 → 0 on scenario 3 between runs. A run that
extracts nothing yields a mostly-`pass` scorecard while proving nothing — the same failure mode as
H4. Consider asserting a floor on `extracted()` per scenario so a silent Observer fails loudly
instead of grading well.

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
