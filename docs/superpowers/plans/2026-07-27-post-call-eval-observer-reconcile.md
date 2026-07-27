# Post-call Eval ↔ Observer Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `evaluate_call` judge the Observer's live answers and top-up only missing fields, instead of silently no-oping when the Observer already wrote `ai_call` answers; gate its auto-retry on the tenant flag; log dropped judge verdicts.

**Architecture:** All behavior changes live in `vera_core/services/post_call_eval.py` (guard removal + judge/top-up reconcile + gated decision), with one new `ReviewReason` enum value, a boolean threaded through `PostCallConsumer` → `EvalDeps`, and a call-recording extension to `FakeLLMClient`. No DB migration (the `review_reason` column is `String(32)`, not a DB enum). No frontend change (the Reason chip humanizes any slug via `statusLabel()`).

**Tech Stack:** Python 3.12, SQLAlchemy async, pytest (+ pytest-asyncio), Docker Postgres integration tests (`just up` + `just migrate` first), `just check` = ruff check + ruff format --check + mypy --strict + pytest.

**Spec:** `docs/superpowers/specs/2026-07-27-post-call-eval-observer-reconcile-design.md`

## Global Constraints

- PHI discipline: log **field paths only**, never answer values (paths are schema constants).
- `just check` must pass on the exact final tree; run it verbatim, never a subset.
- After implementation, run the `/simplify` skill on the change, then re-run `just check` (repo rule).
- Integration tests need local infra: `just up` then `just migrate` (they skip without it).
- mypy is `--strict`: annotate everything you add.
- Branch: `feat/post-call-eval-observer-reconcile` (already created; spec committed).
- Working dir for all commands: `vera-backend/`.

---

### Task 1: Gate the eval's auto-retry on the tenant flag

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py` (ReviewReason, ~line 155)
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py` (`EvalDeps` ~line 67; decision tail ~lines 363–385)
- Modify: `apps/control_plane/src/control_plane/post_call_consumer.py` (constructor ~lines 60–93)
- Modify: `apps/control_plane/src/control_plane/main.py` (`PostCallConsumer(...)` ~line 310)
- Test: `tests/integration/test_post_call_eval.py`

**Interfaces:**
- Consumes: existing `EvalDeps`, `evaluate_call`, `FormStateMachine.can_retry`, `settings.form_auto_retry_enabled`.
- Produces: `EvalDeps.auto_retry_enabled: bool = False`; `ReviewReason.AUTO_RETRY_DISABLED = "auto_retry_disabled"`; `PostCallConsumer(..., auto_retry_enabled: bool = False)` keyword arg. Task 2 builds on the same decision tail unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_post_call_eval.py` (after `test_incomplete_retries_exhausted_goes_to_review`):

```python
async def test_incomplete_retryable_with_auto_retry_disabled_goes_to_review(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Retryable required field + retries remaining, but the tenant auto-retry
    flag is off (EvalDeps default) → no requeue; EXCEPTION_REVIEW with the
    honest reason auto_retry_disabled."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "agent", "are they in network")]
    # LLM extracts nothing → the required ask field stays unsatisfied (retryable).
    llm = FakeLLMClient(extracted=[], verdicts=[])
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)  # flag defaults off

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "auto_retry_disabled"
    assert form.retry_count == 0  # the IN_QUEUE branch never fired
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py::test_incomplete_retryable_with_auto_retry_disabled_goes_to_review -v
```
Expected: FAIL — outcome is `IN_QUEUE` (current code requeues unconditionally), and `"auto_retry_disabled"` doesn't exist yet.

- [ ] **Step 3: Add the enum value**

In `packages/vera_core/src/vera_core/models/enums.py`, inside `ReviewReason` after `USER_ENDED`:

```python
    # Required fields are unsatisfied and retryable, but the tenant has
    # form_auto_retry_enabled off — the eval never auto-redials for them, so
    # the form parks for a human instead of re-queueing.
    AUTO_RETRY_DISABLED = "auto_retry_disabled"
```

- [ ] **Step 4: Add the flag to EvalDeps and gate the decision**

In `packages/vera_core/src/vera_core/services/post_call_eval.py`:

`EvalDeps` gains one field (after `floor`):

```python
@dataclass
class EvalDeps:
    llm: LLMClient
    audit: AuditSink
    livekit: Any
    kms: Any = None
    recording: Any = None
    plan_service: Any = None
    floor: int = REVIEW_CONFIDENCE_FLOOR
    # Mirrors settings.form_auto_retry_enabled — the eval never auto-redials a
    # payer for a tenant that has auto-retry off (same gate the fallback
    # resolver applies). Default False: safe when a caller forgets to wire it.
    auto_retry_enabled: bool = False
```

Replace the decision tail (the `if retryable and sm.can_retry(...)` block through the final `return await _finish(...)`) with:

```python
    if retryable and sm.can_retry(form, tenant_max_retries=tenant.max_retries):
        # Never auto-redial a call a supervisor deliberately ended: route to human
        # review instead of re-queueing (see user_ended above).
        if user_ended:
            return await _finish(
                FormStatus.EXCEPTION_REVIEW,
                written=len(kept),
                reviewed=unsatisfied,
                reason=ReviewReason.USER_ENDED,
            )
        if deps.auto_retry_enabled:
            return await _finish(
                FormStatus.IN_QUEUE, written=len(kept), reviewed=[], reason="retry"
            )
        return await _finish(
            FormStatus.EXCEPTION_REVIEW,
            written=len(kept),
            reviewed=unsatisfied,
            reason=ReviewReason.AUTO_RETRY_DISABLED,
        )
    return await _finish(
        FormStatus.EXCEPTION_REVIEW,
        written=len(kept),
        reviewed=unsatisfied,
        reason=(
            ReviewReason.RETRIES_EXHAUSTED if retryable else ReviewReason.UNSATISFIED_UNASKABLE
        ),
    )
```

- [ ] **Step 5: Wire the flag through the consumer and main.py**

`apps/control_plane/src/control_plane/post_call_consumer.py` — add a keyword arg to `PostCallConsumer.__init__` (after `review_floor: int = 60`):

```python
        review_floor: int = 60,
        auto_retry_enabled: bool = False,
```

and pass it into the `EvalDeps(...)` construction:

```python
        self._deps = EvalDeps(
            llm=llm,
            audit=audit,
            livekit=livekit,
            kms=kms,
            recording=recording,
            plan_service=plan_service,
            floor=review_floor,
            auto_retry_enabled=auto_retry_enabled,
        )
```

`apps/control_plane/src/control_plane/main.py` — in the `PostCallConsumer(...)` call (~line 310), add:

```python
                review_floor=settings.post_call_review_floor,
                auto_retry_enabled=settings.form_auto_retry_enabled,
```

- [ ] **Step 6: Update the three existing tests that exercised the ungated requeue**

In `tests/integration/test_post_call_eval.py`, in `test_incomplete_with_retries_left_requeues`, `test_incomplete_with_retries_left_requeues_clears_review_reason`, and `test_user_canceled_call_never_requeues`, change each `EvalDeps(...)` construction to opt in:

```python
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit, auto_retry_enabled=True)
```

(These tests verify the requeue and the user-ended override — behaviors that now require the flag.)

- [ ] **Step 7: Run the module's tests**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -v
```
Expected: ALL PASS, including the new test.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/models/enums.py \
        packages/vera_core/src/vera_core/services/post_call_eval.py \
        apps/control_plane/src/control_plane/post_call_consumer.py \
        apps/control_plane/src/control_plane/main.py \
        tests/integration/test_post_call_eval.py
git commit -m "feat(post-call): gate eval auto-retry on form_auto_retry_enabled

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Judge + top-up — reconcile the eval with Observer live answers

**Files:**
- Modify: `packages/vera_core/src/vera_core/integrations/llm.py` (`FakeLLMClient`, ~lines 48–72)
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py` (module docstring; steps 1–6, ~lines 140–320)
- Test: `tests/integration/test_post_call_eval.py` (`_SCHEMA_JSON` + two new tests)

**Interfaces:**
- Consumes: `EvalDeps` (incl. Task 1's `auto_retry_enabled`), `FieldAnswer`, `FieldEvaluation`, `current_values_by_path(session, form_id) -> dict[str, Any]` (already imported), `unwrap_value` (from `vera_core.services.field_answers`), `ExtractedField(field_path, value, confidence, evidence_seq)`.
- Produces: `FakeLLMClient.extract_calls: list[list[str]]` and `judge_calls: list[list[ExtractedField]]` (recorded per invocation); `evaluate_call` judge+top-up semantics that Task 3's logging slots into (the verdict-matching loop iterates `to_judge: list[tuple[ExtractedField, FieldAnswer]]`).

- [ ] **Step 1: Make FakeLLMClient record its calls**

In `packages/vera_core/src/vera_core/integrations/llm.py`, replace `FakeLLMClient` with:

```python
class FakeLLMClient:
    """Deterministic test double. Records each call's arguments so tests can
    assert WHAT was extracted/judged (e.g. top-up extraction only receives the
    missing paths)."""

    def __init__(
        self,
        *,
        extracted: list[ExtractedField],
        verdicts: list[JudgeVerdict],
        raise_on_extract: Exception | None = None,
    ) -> None:
        self._extracted = extracted
        self._verdicts = verdicts
        self._raise_on_extract = raise_on_extract
        self.extract_calls: list[list[str]] = []
        self.judge_calls: list[list[ExtractedField]] = []

    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]:
        self.extract_calls.append(list(field_paths))
        if self._raise_on_extract is not None:
            raise self._raise_on_extract
        return list(self._extracted)

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        self.judge_calls.append(list(extracted))
        return list(self._verdicts)
```

- [ ] **Step 2: Add a second (optional) collection path to the test schema**

In `tests/integration/test_post_call_eval.py`, add a `notes` field to `_SCHEMA_JSON["sections"]["coverage"]["fields"]` (beside `in_network`) and a module constant:

```python
                "notes": {
                    "type": "text",
                    "title": "Notes",
                    "role": "ask",
                    "required": False,
                    "prompt": {"ask": "Any additional notes?"},
                },
```

```python
# Second (optional) collection path — used by the judge+top-up tests.
_NOTES_PATH = "sections.coverage.notes"
```

- [ ] **Step 3: Write the two failing tests**

Add to `tests/integration/test_post_call_eval.py`:

```python
def _observer_answer(
    ctx: _SeedCtx, path: str, value: str, *, confidence: int = 90, evidence_seq: int = 1
) -> FieldAnswer:
    """A FieldAnswer as worker_events._handle_call_answer_recorded writes it:
    ai_call source with the live call's id, current."""
    return FieldAnswer(
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        field_path=path,
        value={"value": value},
        source=AnswerSource.AI_CALL.value,
        confidence=confidence,
        evidence_seq=evidence_seq,
        is_current=True,
    )


async def test_observer_answers_are_judged_and_missing_paths_topped_up(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """Regression for the Observer/eval conflict (2026-07-27 E2E): live ai_call
    answers must NOT no-op the eval. The eval judges them, extracts only the
    still-missing paths, judges those too, and finishes with a real reason."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    await ctx.session.flush()

    turns = [
        TranscriptTurn(0, "agent", "are they in network"),
        TranscriptTurn(1, "user", "yes in network, no notes"),
    ]
    llm = FakeLLMClient(
        extracted=[ExtractedField(_NOTES_PATH, "no notes", 80, 1)],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    # Top-up extraction asked ONLY for the missing path.
    assert llm.extract_calls == [[_NOTES_PATH]]
    # One combined judge pass over observer answer + topped-up answer.
    assert len(llm.judge_calls) == 1
    assert {ef.field_path for ef in llm.judge_calls[0]} == {ctx.collection_path, _NOTES_PATH}

    # Not a no-op: the form transitioned with a real reason (required field
    # satisfied by the observer answer → ready_for_review).
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "ready_for_review"

    # FieldEvaluation rows exist for BOTH answers.
    answers = (
        (
            await ctx.session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == ctx.form_id,
                    FieldAnswer.source == AnswerSource.AI_CALL.value,
                    FieldAnswer.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {a.field_path for a in answers} == {ctx.collection_path, _NOTES_PATH}
    for a in answers:
        evals = (
            (
                await ctx.session.execute(
                    select(FieldEvaluation).where(FieldEvaluation.answer_id == a.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(evals) == 1, f"no FieldEvaluation for {a.field_path}"


async def test_nothing_missing_skips_extraction_and_judges_observer_answers(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
) -> None:
    """When the Observer answered every collection path, the eval must not
    spend an extract call — judge-only."""
    ctx = seeded_ai_processing_form
    ctx.session.add(_observer_answer(ctx, ctx.collection_path, "in-network"))
    ctx.session.add(_observer_answer(ctx, _NOTES_PATH, "none", evidence_seq=2))
    await ctx.session.flush()

    turns = [TranscriptTurn(0, "user", "yes in network, no notes")]
    llm = FakeLLMClient(
        extracted=[],
        verdicts=[
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
            JudgeVerdict(_NOTES_PATH, True, 70, "no notes"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session,
        deps,
        tenant_id=ctx.tenant_id,
        form_id=ctx.form_id,
        call_id=ctx.call_id,
        turns=turns,
    )

    assert llm.extract_calls == []  # no top-up needed
    assert len(llm.judge_calls) == 1
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    form = await ctx.reload_form()
    assert form.review_reason == "ready_for_review"
```

- [ ] **Step 4: Run the new tests to verify they fail**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py::test_observer_answers_are_judged_and_missing_paths_topped_up tests/integration/test_post_call_eval.py::test_nothing_missing_skips_extraction_and_judges_observer_answers -v
```
Expected: FAIL — the answer-existence guard no-ops both (`review_reason` stays None, no `FieldEvaluation` rows, `extract_calls == []` in the first test but for the wrong reason: the whole eval was skipped, so `judge_calls == []` too).

- [ ] **Step 5: Implement judge + top-up in evaluate_call**

In `packages/vera_core/src/vera_core/services/post_call_eval.py`:

a. Add `unwrap_value` to the existing `vera_core.services.field_answers` import.

b. **Delete the step-1 idempotency guard** (the `# (1) Idempotency guard` comment through its `return EvalOutcome(...)` — currently lines 140–152). Rationale stays in the module docstring (step d). Redelivery safety: a committed eval already moved the form out of AI_PROCESSING (status guard no-ops), a rolled-back eval left no partial state.

c. Replace the extraction block (step 4-5, from `paths = doc.collection_paths()` through the `kept` loop + flush) with:

```python
    paths = doc.collection_paths()

    # (4a) The Observer's live answers for THIS call (worker_events persists
    # ai_call answers during the call since 4f0b8a9). They ARE the extraction
    # for whatever the call covered — the eval judges them instead of
    # re-extracting, and tops up only what is still missing.
    observer_rows = (
        (
            await session.execute(
                select(FieldAnswer).where(
                    FieldAnswer.form_id == form_id,
                    FieldAnswer.call_id == call_id,
                    FieldAnswer.source == AnswerSource.AI_CALL.value,
                    FieldAnswer.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    observer_pairs: list[tuple[ExtractedField, FieldAnswer]] = [
        (
            ExtractedField(
                field_path=row.field_path,
                value=str(unwrap_value(row.value)),
                confidence=row.confidence or 0,
                evidence_seq=row.evidence_seq or 0,
            ),
            row,
        )
        for row in observer_rows
    ]

    # (4b) Top-up extraction: only paths with no current answer AT ALL — an
    # intake / human / prior-attempt answer is not missing, and the LLM must
    # never supersede one (extracted paths are filtered back to this set).
    answered = set((await current_values_by_path(session, form_id)).keys())
    missing = [p for p in paths if p not in answered]
    extracted: list[ExtractedField] = []
    if missing:
        try:
            extracted = await deps.llm.extract(field_paths=missing, turns=turns)
        except Exception as exc:
            logger.error(
                "post_call_eval: LLM extract failed for form %s — routing to "
                "EXCEPTION_REVIEW (%s: %s)",
                form_id,
                type(exc).__name__,
                exc,
            )
            return await _finish(
                FormStatus.EXCEPTION_REVIEW, written=0, reviewed=[], reason=ReviewReason.LLM_ERROR
            )
    token_fields = [ef.field_path for ef in extracted if has_phi_token(ef.value)]
    clean = [ef for ef in extracted if not has_phi_token(ef.value)]
    # Keep only what was asked for: a hallucinated path must not supersede an
    # intake/human answer (top-up semantics).
    requested = set(missing)
    clean = [ef for ef in clean if ef.field_path in requested]
    # The LLM may emit the same field_path twice; keep the last occurrence. Two
    # inserts for one path would violate the fa_current_uq partial unique index
    # (the batch demote runs before the inserts) and poison-loop the job.
    clean = list({ef.field_path: ef for ef in clean}.values())
    # Demote the outgoing current rows in one statement BEFORE adding their
    # replacements, so the merge invariant (one current row per path) holds at flush.
    await _demote_current(session, form_id, [ef.field_path for ef in clean])
    kept: list[tuple[ExtractedField, FieldAnswer]] = []
    for ef in clean:
        answer = FieldAnswer(
            tenant_id=tenant_id,
            form_id=form_id,
            call_id=call_id,
            field_path=ef.field_path,
            value={"value": ef.value},
            source=AnswerSource.AI_CALL.value,
            confidence=ef.confidence,
            evidence_seq=ef.evidence_seq,
            evidence=evidence_text(turns, ef.evidence_seq),
            is_current=True,
        )
        session.add(answer)
        kept.append((ef, answer))
    await session.flush()
```

d. Change the judge block (step 6) to run over BOTH batches:

```python
    # (6) One judge pass over the Observer's answers + the topped-up ones. The
    # verdicts feed the satisfaction check below via load_field_status —
    # nothing is decided per-field here.
    to_judge: list[tuple[ExtractedField, FieldAnswer]] = observer_pairs + kept
    if to_judge:
        try:
            raw_verdicts = await deps.llm.judge(
                extracted=[ef for ef, _ in to_judge], turns=turns
            )
        except Exception as exc:
            logger.error(
                "post_call_eval: LLM judge failed for form %s — routing to "
                "EXCEPTION_REVIEW (%s: %s)",
                form_id,
                type(exc).__name__,
                exc,
            )
            return await _finish(
                FormStatus.EXCEPTION_REVIEW,
                written=len(kept),
                reviewed=[ef.field_path for ef, _ in to_judge],
                reason=ReviewReason.LLM_ERROR,
            )
        verdicts = {v.field_path: v for v in raw_verdicts}
        for ef, answer in to_judge:
            v = verdicts.get(ef.field_path)
            if v is not None:
                session.add(
                    FieldEvaluation(
                        tenant_id=tenant_id,
                        answer_id=answer.id,
                        confidence=v.confidence,
                        evidence=v.evidence,
                        supported=v.supported,
                    )
                )
        await session.flush()
```

(Note: `if to_judge:` replaces the old implicit behavior — with no answers at all there is nothing to judge; `FakeLLMClient` guards `judge` on empty input in prod code too, but skipping the call entirely keeps `judge_calls` clean and saves an LLM round-trip.)

e. Update the module docstring's behavior list (top of file) to describe judge + top-up:

```python
"""Post-call eval: judge the Observer's live ai_call answers for the finished
call, extract only the still-missing collection paths from the transcript
(top-up), judge those too, and decide the form's terminal status. Pure helpers
here; Redelivery safety comes from the status guard + single-transaction
atomicity (a committed eval already left AI_PROCESSING; a rolled-back one left
no partial state) — there is deliberately no answer-existence guard: the
Observer writes ai_call answers DURING the call, so their presence proves
nothing about whether the eval ran.
"""
```

- [ ] **Step 6: Run the new tests to verify they pass**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py::test_observer_answers_are_judged_and_missing_paths_topped_up tests/integration/test_post_call_eval.py::test_nothing_missing_skips_extraction_and_judges_observer_answers -v
```
Expected: PASS.

- [ ] **Step 7: Run the whole module — existing tests must stay green**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -v
```
Expected: ALL PASS. Notes on three existing tests:
- `test_redelivery_is_a_noop` now passes via the **status guard** (the first eval transitioned the form out of AI_PROCESSING) — same observable behavior, different mechanism. If it seeded a resolved status manually it still passes unchanged.
- `test_llm_failure_routes_to_exception_review` uses `raise_on_extract` — with the new flow extraction still runs (the seeded form has no answers, so both paths are missing) and the error routing is unchanged.
- `test_evaluate_call_writes_answers_and_parks_for_review` asserts `after_state == {path: "in-network"}` — the optional `notes` path stays unanswered there, so the snapshot is unchanged.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/integrations/llm.py \
        packages/vera_core/src/vera_core/services/post_call_eval.py \
        tests/integration/test_post_call_eval.py
git commit -m "fix(post-call): judge Observer live answers, top-up missing fields

The eval's answer-existence idempotency guard predated the Observer
(4f0b8a9): any live ai_call answer made the eval no-op without a status
transition, so forms stranded until the sweeper stamped not_evaluated and
judge verdicts never existed. Remove the guard (status guard + transaction
atomicity already cover redelivery), judge the Observer's answers, and
extract only still-missing paths.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Warn when judge verdicts don't match judged paths

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py` (the verdict-matching loop from Task 2 step 5d)
- Test: `tests/integration/test_post_call_eval.py`

**Interfaces:**
- Consumes: Task 2's `to_judge: list[tuple[ExtractedField, FieldAnswer]]` and `verdicts: dict[str, JudgeVerdict]`.
- Produces: log-only change; no API/signature changes.

- [ ] **Step 1: Write the failing test**

```python
async def test_verdict_path_mismatch_is_logged_not_silent(
    seeded_ai_processing_form: _SeedCtx,
    fake_audit: _FakeAuditSink,
    fake_livekit: _FakeLiveKit,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A judge verdict whose field_path matches no judged answer must be
    WARN-logged (paths only — never values), and the matching verdicts must
    still be written. Silent drops made 'judge never ran' and 'judge output
    mismatched' indistinguishable (2026-07-27 E2E)."""
    ctx = seeded_ai_processing_form
    turns = [TranscriptTurn(0, "user", "yes in network")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(ctx.collection_path, "in-network", 92, 0)],
        verdicts=[
            JudgeVerdict("sections.coverage.bogus_path", True, 90, "??"),
            JudgeVerdict(ctx.collection_path, True, 88, "yes in network"),
        ],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    with caplog.at_level("WARNING", logger="vera_core.services.post_call_eval"):
        await evaluate_call(
            ctx.session,
            deps,
            tenant_id=ctx.tenant_id,
            form_id=ctx.form_id,
            call_id=ctx.call_id,
            turns=turns,
        )

    warnings = [r for r in caplog.records if "judge verdict" in r.getMessage()]
    assert len(warnings) == 1
    assert "sections.coverage.bogus_path" in warnings[0].getMessage()

    # The matching verdict was still written.
    answer = (
        await ctx.session.execute(
            select(FieldAnswer).where(
                FieldAnswer.form_id == ctx.form_id,
                FieldAnswer.field_path == ctx.collection_path,
                FieldAnswer.is_current.is_(True),
            )
        )
    ).scalar_one()
    evals = (
        (
            await ctx.session.execute(
                select(FieldEvaluation).where(FieldEvaluation.answer_id == answer.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(evals) == 1
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py::test_verdict_path_mismatch_is_logged_not_silent -v
```
Expected: FAIL — no warning is logged (`len(warnings) == 0`).

- [ ] **Step 3: Implement the warning**

In the verdict-matching loop (Task 2 step 5d), after `verdicts = {v.field_path: v for v in raw_verdicts}`, add:

```python
        judged_paths = {ef.field_path for ef, _ in to_judge}
        unmatched_verdicts = sorted(set(verdicts) - judged_paths)
        unjudged_answers = sorted(judged_paths - set(verdicts))
        if unmatched_verdicts or unjudged_answers:
            # Paths only — schema constants, never answer values (PHI rule).
            logger.warning(
                "post_call_eval: judge verdict/path mismatch for form %s — "
                "%d verdict(s) match no judged answer (%s); "
                "%d judged answer(s) got no verdict (%s)",
                form_id,
                len(unmatched_verdicts),
                unmatched_verdicts,
                len(unjudged_answers),
                unjudged_answers,
            )
```

- [ ] **Step 4: Run the module's tests**

```bash
cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/services/post_call_eval.py \
        tests/integration/test_post_call_eval.py
git commit -m "fix(post-call): warn on judge verdict/path mismatch instead of silent drop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Full gate + simplify pass

**Files:**
- No new files; whole-tree verification.

- [ ] **Step 1: Run the full CI gate verbatim**

```bash
cd vera-backend && just check
```
Expected: lint (ruff check + format --check), mypy --strict, and pytest all green. If integration tests skip, run `just up && just migrate` first and re-run.

- [ ] **Step 2: Run the /simplify skill on the change (repo rule)**

Invoke `/simplify` scoped to the branch diff (`post_call_eval.py`, `post_call_consumer.py`, `main.py`, `enums.py`, `llm.py`, `test_post_call_eval.py`). Behavior-preserving cleanups only.

- [ ] **Step 3: Re-run the gate on the exact final tree**

```bash
cd vera-backend && just check
```
Expected: green. If /simplify changed anything, commit:

```bash
git add -A && git commit -m "refactor(post-call): simplify pass on eval reconcile change

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/post-call-eval-observer-reconcile
```

PR body should link the spec (`docs/superpowers/specs/2026-07-27-post-call-eval-observer-reconcile-design.md`) and name the two runtime prerequisites for seeing the fix on test: `VERA_GCP_PROJECT` set (eval consumer boots) and, if the retry path should fire, `VERA_FORM_AUTO_RETRY_ENABLED=true`.
