# Eval run — contextual owed-question list (branch `fix/task-complete-and-gap-pass-question-preparatino`)

**Date:** 2026-08-13
**Tree:** `e041d0b7` (8 implementation commits on top of merge `239a5720` with `origin/dev`)
**Command:** `VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs`
**Result line:** `18 passed, 1 skipped, 43 deselected, 2 xpassed, 32 warnings in 1036.07s (0:17:16)`
**Log:** scratchpad `evals.log`, 818 lines (not committed — contains full transcripts)

## Headline

All six scenarios printed `overall FAIL`. **`overall` is not an aggregate of the other
dimensions** — it is its own LLM question (`judge.py:79`):

> *"Taking the call as a whole, would you put this in front of a real payer representative?"*

An evaluator that has just listed one flaw answers "no". So `overall FAIL` means *"at least one
thing was off"*, not *"the call was broken"*. Two facts fix the scale:

1. **`overall pass` has never been recorded in this repository.** `rg -rn "overall +pass" docs/`
   returns nothing across every prior eval write-up. There is no green baseline to regress from.
2. **Every dimension failure in this run falls into four buckets, all four already open and
   documented** in `2026-07-30-call-flow-eval-findings-remediation.md` (P1, P2, P8, P10).

The two dimensions this branch governs — `gap_conduct` and `scope_discipline` — **passed**.

## Scorecard matrix

15 dimension failures across 60 dimension verdicts (plus the 6 `overall` opinions = 21 `FAIL`
lines in the log).

| dimension | S1 cooperative | S2 mandate | S3 impossible max | S4 remaining max | S5 not active | S6 family |
|---|---|---|---|---|---|---|
| flow_rules | n/a | n/a | n/a | n/a | **pass** | n/a |
| contradictions | n/a | FAIL | pass | n/a | n/a | n/a |
| task_handoffs | pass | FAIL | pass | pass | pass | pass |
| tool_calls | FAIL | FAIL | FAIL | FAIL | pass | FAIL |
| ivr_navigation | pass | pass | pass | pass | pass | pass |
| question_coverage | FAIL | pass | pass | pass | pass | FAIL |
| **scope_discipline** | **pass** | n/a | n/a | n/a | n/a | n/a |
| answer_handling | FAIL | FAIL | FAIL | pass | pass | pass |
| **gap_conduct** | **pass** | n/a | n/a | n/a | n/a | n/a |
| closing | pass | FAIL | pass | FAIL | FAIL | pass |
| overall | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |

Answers extracted per scenario: **97 / 4 / 7 / 6 / 3 / 6** — none zero, so the Observer
contributed on every call (a zero would have made most dimensions meaningless).

## Why only one scenario tests this change

`test_handoff_flow.py:588` — `@pytest.mark.skipif(not full_walk_enabled(), reason="the gap pass
needs the unfocused plan")`. Only **S1 (cooperative rep, full walk)** exercises the gap pass.

The other five run `focus_call_plan` (`conftest.py:362,366`, *"production's own FOCUSED-retry
path"*) — the narrowing this branch **deliberately does not fix**. The evaluator flagged it
itself, unprompted, in five of six: `scope_discipline n/a — Plan narrowing defect applies to this
run`.

So S2–S6's failures sit on the known-broken retry path, and their `scope_discipline` /
`gap_conduct` are `n/a` because neither is reachable there.

## The 15 failures, bucketed

| # | bucket | count | prior record |
|---|---|---|---|
| 1 | `tool_calls` — premature `task_complete` | 5 | **P1 / P10.** Run 2: *"called press_keypad four times in a row and called task_complete twice consecutively"* |
| 2 | `closing` — signed off twice | 3 | **P2.** *"Run 2 produced **three** sign-offs in two separate scenarios"* |
| 3 | `answer_handling` — re-asked what was just answered | 3 | P1-caused; line 484 lists *"the P1-caused `answer_handling` / `task_handoffs` failures"* as pending |
| 4 | `question_coverage` — two questions in one turn | 2 | Documented class; the rubric makes *"asking several at once"* a fail |
| 5 | `task_handoffs` — re-asked rep name after handoff | 1 | Same P1 bucket as #3 |
| 6 | `contradictions` — pushed back a second time | 1 | Adjacent to the `ReAsk` note at line 57 |

Nothing in this list touches owed-question rendering, which is the whole of this branch's change.

### Bucket 1 deserves a correction to how it reads

`tool_calls FAIL: "called task_complete prematurely"` is the single most common failure (5 of 6).
It reads as a broken guard. It is the opposite:

```
guard refusals fired : 20 × task_complete + 4 × gap_complete = 24
calls reaching WrapUpAgent : 6 / 6
```

The guard caught **every** premature attempt and the call recovered each time — no deadlock, no
stranded task. The evaluator is scoring the model's first instinct, which the runtime then
overrode. That is P1 (the model's eagerness), not a completion-guard defect.

## Evidence the change works

From the S1 transcript — the new refusal, mid-call, on two services that **share one CPT code**
(58555), structurally the same collision as the reported `89337` case:

```
<- Not yet — these required questions of the current task have no answer on file...
* First settle which applies: ...ambulatory surgical center services — is that billed as
  professional or facility? (ASC Professional Services or ASC Facility — only one applies)

ASC Professional Services [CPT 58555; ICD ten Z31.41]:
  1. Is CPT code 58555 for ambulatory surgical center professional services covered...?

ASC Facility [CPT 58555; ICD ten Z31.41]:
  2. Is CPT code 58555 for the ambulatory surgical center facility covered...?
```

Service crumbs with codes, both branches distinguished, and the routing question retained because
both were owed — exactly the designed output. A later refusal carried its gate inline
(`only if "Lifetime Maximum" is none of "No Limit", "Unlimited"`), and VERA asked that question
next.

The strongest signal is the gap agent's own closing reason:

> *"The representative confirmed CPT 58555 for ASC professional services is not covered, **and
> since it is not covered, the remaining conditional follow-ups do not apply**."*

The two-tier list worked as designed: the agent saw the pre-loaded conditional follow-ups, judged
the gate false, and declined to ask them. This was the identified risk of dropping *"the list is
the complete set"* from `_gap_block` — that the agent would ask conditionals unconditionally. The
opposite happened.

## What this run does NOT establish

- **No baseline.** These FAILs match documented open defects and no `overall pass` has ever been
  recorded, but the same run has not been executed on merge-base `239a5720`. Attribution is by
  category match, not by measurement. A baseline run costs ~17 min and the same LLM spend.
- **`still_needed` never rendered** (0 occurrences). No partially-answered fan-out arose. It is
  covered by unit tests against the real 8-code diagnostic panel, but is unexercised end to end.
- **`VERA_EVALS_JUDGE_STRICT` was unset**, so verdicts were printed, not asserted. Exit code 0 is
  not a verdict — the single skip is that opt-in gate.
- **The harness is not a live call.** No STT, no real DTMF, and extraction settles between turns,
  so rules fire more reliably than in production. A browser-callee call is still required.

## Recommended next step

A live browser-callee call driven into a `task_complete` refusal and through the gap pass, since
that is the check the harness explicitly does not replace. The baseline eval run is optional and
only buys attribution for failures already traced to open findings P1/P2/P8/P10.
