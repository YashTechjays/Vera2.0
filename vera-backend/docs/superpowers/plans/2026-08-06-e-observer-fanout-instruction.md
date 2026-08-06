# Plan E — teach the extractor that one answer can cover several field paths

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the rep answers for a set of codes at once ("all of those are covered, twenty
dollar copay"), the Observer must emit one row per covered path instead of one or two.

**Architecture:** One sentence added to `_extraction_instructions`. **The flat
`- <path>: <title>` list stays exactly as it is.**

**Tech Stack:** Python 3.12, pytest.

## Global Constraints

- The extractor's output contract is unchanged: a JSON array of
  `{field_path, value, confidence}`, no prose, no code fence.
- The transcript handed to the chain is PHI. Do not add logging; the existing span already sets
  `record_exception=False` and `set_status_on_exception=False` for that reason
  (`observer.py:115-120`).
- Extraction runs through `vera_core.llm.ResilientLLM` — do not instantiate a provider SDK.

**Depends on:** nothing. Ship it independently.

---

## Why the field list stays flat

The Observer's job is `transcript → [{field_path, value, confidence}]`. A flat list is the right
interface for that, and the **paths already self-disambiguate**:
`…labs_xray_ultrasound.cpt_58340.copay` carries the code even though 24 entries share the title
`Copay ($)`. Grouping the listing would add tokens without changing the output contract and
would risk muddying the strict *"Use only these field_path values"* rule.

An earlier draft of this work proposed restructuring the list by service. **That was
over-scoped and is explicitly rejected here.**

What is genuinely missing is a fan-out instruction. `_extraction_instructions`
(`observer.py:125-136`) says *"Return ONLY the fields below that the representative has
**clearly** answered"*, which reads conservatively — a blanket "all of those are covered" can
yield 2 rows where 24 are correct. Reps volunteer blanket answers **today**, against a prompt
that asks per code, so this is worth landing before and independently of Plan B. Plan B makes it
the normal path rather than an occasional one.

---

## File Structure

- **Modify** `apps/agent_worker/src/agent_worker/observer.py` (`_extraction_instructions`, `:125-136`)
- **Test:** `apps/agent_worker/tests/unit/test_observer.py`

**Interfaces:** none changed. `_extraction_instructions(task: PlanTask) -> str` keeps its
signature; `AnswerExtractor` and `ExtractedAnswer` are untouched.

---

### Task 1: add the fan-out sentence

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py`
- Test: `apps/agent_worker/tests/unit/test_observer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_extraction_instructions_explain_that_one_answer_can_cover_many_paths() -> None:
    task = PlanTask(
        task_key="diagnostic_coverage",
        title="Diagnostic Coverage",
        prompt="Diagnostic.",
        fields=[
            _field("sections.d.labs.cpt_58340.copay", "Copay ($)"),
            _field("sections.d.labs.cpt_82670.copay", "Copay ($)"),
        ],
    )
    text = _extraction_instructions(task)
    assert "one row per" in text
    # the flat path list is the contract and must not have been regrouped
    assert "- sections.d.labs.cpt_58340.copay: Copay ($)" in text
    assert "- sections.d.labs.cpt_82670.copay: Copay ($)" in text
```

Reuse the module's existing `PlanFieldDescriptor` helper; if `test_observer.py` has none, add:

```python
def _field(path: str, title: str) -> PlanFieldDescriptor:
    return PlanFieldDescriptor(path=path, title=title, type="text", role="ask")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -v -k fan
```

Expected: FAIL on `assert "one row per" in text`.

- [ ] **Step 3: Write minimal implementation**

In `_extraction_instructions`, append one element to the opening `lines` list — after the
existing instruction string and **before** the `for f in task.fields` loop, so the field list
stays last:

```python
        "A single answer can cover several field_paths. When the representative answers for a "
        "set of codes or services at once (\"all of those\", \"same for the rest\", \"they all "
        "have the same copay\"), emit one row per field_path it covers, not just the one that "
        "was named aloud.",
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py apps/agent_worker/tests/unit/test_observer.py
git commit -m "fix(observer): extract one row per path when the rep answers for a set of codes"
```

---

### Task 2: verify against a real transcript window

**Files:** none modified.

- [ ] **Step 1: Full gate**

```bash
just check
```

- [ ] **Step 2: Live-ish check**

Run the eval harness and inspect the extracted-answer count on the cooperative-rep scenario,
where the simulated rep does give blanket answers:

```bash
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

Compare `answers extracted` against the baseline table in
`2026-07-30-call-flow-eval-findings-remediation.md` (44 → 50 → 45 across three runs on the
cooperative scenario). Expect equal or higher; a drop means the new sentence is causing
over-emission and should be tightened, not kept.

**Do not overclaim from a green run** — the harness has no STT and extraction settles between
turns, so rules fire more reliably than on a real call.

---

## Out of scope

- Regrouping or restructuring the field list (explicitly rejected above).
- Confidence thresholds and the `_parse_extraction` tolerant-JSON path.
- The `_MAX_WINDOW_TURNS = 24` transcript window.
