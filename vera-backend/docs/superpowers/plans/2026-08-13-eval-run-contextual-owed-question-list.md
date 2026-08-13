# Eval run — contextual owed-question list (branch `fix/task-complete-and-gap-pass-question-preparatino`)

**Date:** 2026-08-13
**Tree:** `e041d0b7` — **STALE, and deliberately kept so.** This run predates `b536592a` (the
pre-refusal drain) and `f59702af` (the refusal-delivery rewrite), i.e. the two riskiest commits on
the branch. Everything below describes the tree named here: in particular the quoted transcript
still shows the retired `Not yet —` opener, and the refusals it captured had **no drain**, so the
tool-call durations are all 0.00s. Read it as the *pre-drain* baseline, not as evidence about the
shipped behaviour — for that see the live-call section at the bottom.
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

The agent saw the pre-loaded conditional follow-ups, judged the gate false, and declined to ask
them.

**Superseded — this observed only the favourable direction.** At this tree `_gap_block` told the
agent that anything marked `Ask only if …` was a follow-up to defer, and had dropped *"the list is
the complete set"*. But `render_panels` prints that same prose on **required** questions whose
condition already holds (measured: 4 of 5 exploded questions on `embryo_biopsy`, including copay,
prior auth and cycle limit), so the instruction could equally have deferred owed questions — the
eval simply never hit a case where the gate was true. Code review caught it; the wording now tells
the agent to evaluate the condition, and the completeness bound is back.

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

---

# Live calls after the eval (the shipped behaviour)

Three browser-callee calls, each on a later tree than the eval above.

| Trace | Model | Tree | Result |
|---|---|---|---|
| `2650888a…` | gemini-2.5-flash | pre-drain | 6 refusals, **4 provably stale** (answers landed 0.8–2.8s later); agent re-asked finished services; 7 apologies/corrections |
| `cea003d0…` | gemini-2.5-flash | + `b536592a` | drain visible (refusals 0.74–4.00s; 6 of 8 *accepted* calls paid 0.44–3.54s and were rescued); **no backward re-asks**; apologies 7 → 2, both legitimate |
| `2fb9915e…` | gemini-3.6-flash | + `f59702af` | 6 refusals, all new wording, **zero narration leaks**; **0 still-owed questions** at question and field level, `completion_pct` 100 |

Two facts the drain measurement settled, both against the initial assumption:

- `drain_pending` only awaits passes that **already exist**. The constant's own comment says
  scheduling lag before a pass starts is *"unreachable by any timeout here"* — so the 31.5s FET
  case was never fixable by draining, and the reworded message is what covers that class.
- The drain is doing more than the refusal count suggests: refusals went 6/14 → 7/15 (flat), but
  6 of 8 accepted calls paid drain time, i.e. the drain converted would-be refusals into clean
  completions.

`2fb9915e…` also showed `form status=exception_review`, `review_reason=not_evaluated` — the
post-call eval consumer is gated on `VERA_GCP_PROJECT`, which is unset locally, so that call got
no post-call verification pass (`verified_pct` None while `completion_pct` is 100). Unrelated to
this branch; tracked separately in the vault note *Post-Call Eval Bypasses the ResilientLLM
Pattern*.
