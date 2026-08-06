# Confirm-Role Leaves With No Value On File — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `role="confirm"` leaf has no prefilled value, the voice agent asks an authored open question instead of speaking the literal string `{{value}}` or inventing a value — and a gate whose own input is not yet answered stops being reported to the agent as "excluded".

**Architecture:** The confirm sentence moves from compile time (once per schema version, no values available) to fuse time (once per form, values known). `prompting.py` emits a `{{confirm:<leaf path>}}` slot; `PrefillFuser` expands it to either `confirm — <sentence with the value>` or `ask — <authored open question>`. Separately, `plan_runtime.py` splits its two-way gate partition into three so an *undecided* gate is no longer reported as *excluded*.

**Tech Stack:** Python 3.12 (`<3.13`), pydantic v2 models as the DSL grammar, `uv` workspace (`vera_core` → `control_plane` + `agent_worker`), pytest, ruff, mypy --strict, `just` for all gates. Google Apps Script for the intake sheet.

**Spec:** `docs/superpowers/specs/2026-08-05-confirm-role-empty-value-design.md`

## Global Constraints

- **PHI:** never log, print, or trace a field **value**. Log field **paths**, counts, and `type(exc).__name__` only. Every new log line and every validation error message in this plan carries paths/counts only.
- **Never hand-edit `data/form_schemas/*.json`.** Change the catalog module, then run `just compile-schemas`. A freshness test fails CI on drift.
- **Run `just check` verbatim** before claiming any task done — it is `lint` (ruff **check** + ruff **format --check**, two different gates) + `typecheck` (mypy --strict) + `test` (pytest). Never a hand-picked subset.
- **Code style:** PEP 695 type params (`def f[T]`), never `TypeVar`/`Generic[T]` — ruff rejects them.
- **Async runtime is asyncio only.** Never `import anyio`.
- **Comments:** default to none. Add one only where it explains a non-obvious constraint or trade-off the code cannot, and keep it to one line. Docstrings stay a single sentence.
- **Commit messages:** do **not** add a `Co-Authored-By: Claude` trailer.
- **Git remote is Bitbucket, not GitHub.** Branches track `origin/dev`, so push with an explicit refspec: `git push origin HEAD:refs/heads/<branch>`. A bare `git push` targets dev. `gh` cannot open PRs here.
- Work on branch `fix/spouse-name-empty-issue` (already checked out).
- All paths below are relative to `vera-backend/` unless stated otherwise. Run all commands from `vera-backend/`.

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `packages/vera_core/src/vera_core/forms/dsl.py` | Grammar + validation of `prompt.ask`/`prompt.confirm` and placeholder tokens | 1, 2 |
| `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` | Authored ask fallbacks for 3 confirm leaves | 1 |
| `packages/vera_core/src/vera_core/forms/catalog/disease_only.py` | Authored ask fallback for 1 confirm leaf | 1 |
| `packages/vera_core/src/vera_core/forms/prompting.py` | Emit the confirm slot; expose gate-chain prose; reject `{{value}}` in tenant prompt docs | 2, 4, 5 |
| `packages/vera_core/src/vera_core/forms/call_plan.py` | `_render_value` N/A suppression; slot expansion; `gate_text` on the descriptor | 3, 4, 5 |
| `apps/agent_worker/src/agent_worker/plan_runtime.py` | Three-state gating block | 5 |
| `packages/vera_core/src/vera_core/forms/intake.py` | Enum membership validation | 6 |
| `apps/control_plane/src/control_plane/api/v1/patient_forms.py` | Wire the enum check as a 422 | 6 |
| `data/ibv_infertility_appscript.js` | Revive the dead spouse-required guard | 7 |
| `data/form_schemas/*.json` | Generated — `just compile-schemas` only | 1, 4 |
| `tests/unit/forms/snapshots/*.prompt.txt` | Generated — re-recorded | 4 |

---

### Task 1: Allow and require `prompt.ask` on confirm-role leaves

The validator currently **raises** on `prompt.ask` for any role other than `"ask"` (`dsl.py:322-323`), so adding an ask fallback to a confirm leaf is rejected today. This task flips that from forbidden to required, and authors the four asks in the same commit — the validator rule and the catalog content must land together or the tree is red.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py:319-327`
- Modify: `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py:190-195, 206-208, 365-368`
- Modify: `packages/vera_core/src/vera_core/forms/catalog/disease_only.py:164-166`
- Test: `tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: nothing.
- Produces: every `role="confirm"` leaf in every catalog now has non-`None` `leaf.prompt.ask` **and** `leaf.prompt.confirm`. Tasks 4 relies on both being present.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/forms/test_schema_dsl.py`. First add `FieldPrompt` and `Leaf` to the existing `from vera_core.forms.dsl import (...)` block (lines 12-20) — the file does not import them yet. `ValidationError` and `pytest` are already imported.

```python
def test_confirm_leaf_accepts_prompt_ask() -> None:
    leaf = Leaf(
        type="text",
        title="Policy / Member ID",
        role="confirm",
        prompt=FieldPrompt(
            confirm="I have the member ID as {{value}} — can you confirm that is correct?",
            ask="Can I get the member ID for this policy?",
        ),
    )
    assert leaf.prompt is not None
    assert leaf.prompt.ask == "Can I get the member ID for this policy?"


def test_confirm_leaf_without_prompt_ask_is_rejected() -> None:
    with pytest.raises(ValidationError, match="confirm field needs prompt.ask"):
        Leaf(
            type="text",
            title="Policy / Member ID",
            role="confirm",
            prompt=FieldPrompt(confirm="I have the member ID as {{value}} — right?"),
        )


def test_prompt_ask_still_rejected_on_context_role() -> None:
    with pytest.raises(ValidationError, match="prompt.ask on role context"):
        Leaf(
            type="text",
            title="Spouse Gender",
            role="context",
            prompt=FieldPrompt(ask="What is the spouse's gender?"),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py -k "prompt_ask or confirm_leaf" -v
```

Expected: `test_confirm_leaf_accepts_prompt_ask` FAILS with `ValidationError: prompt.ask on role confirm`; `test_confirm_leaf_without_prompt_ask_is_rejected` FAILS because no such error is raised.

- [ ] **Step 3: Change the validator**

In `packages/vera_core/src/vera_core/forms/dsl.py`, replace lines 322-323 and add the new requirement after line 327:

```python
            if self.prompt.ask is not None and self.role not in ("ask", "confirm"):
                raise ValueError(f"prompt.ask on role {self.role}")
```

```python
        if self.role == "confirm" and not (self.prompt and self.prompt.ask):
            # The open question spoken when no value is on file to read back.
            raise ValueError("confirm field needs prompt.ask")
```

- [ ] **Step 4: Author the four ask fallbacks**

`catalog/ibv_standard.py` — `spouse_partner_name` (replace the `prompt=FieldPrompt(...)` block at lines 190-195):

```python
                    prompt=FieldPrompt(
                        confirm=(
                            "Can we also check the spouse on the plan? I have the spouse listed "
                            "as {{value}} — can you confirm that is correct?"
                        ),
                        ask=(
                            "Can we also check the spouse on the plan? "
                            "Can I get the spouse's full name?"
                        ),
                    ),
```

`catalog/ibv_standard.py` — `spouse_partner_dob` (lines 206-208):

```python
                    prompt=FieldPrompt(
                        confirm="And the spouse's date of birth I have is {{value}} — is that right?",
                        ask="And what is the spouse's date of birth?",
                    ),
```

`catalog/ibv_standard.py` — `policy_number` (lines 365-368):

```python
                prompt=FieldPrompt(
                    confirm="I have the member ID as {{value}} — can you confirm that is correct?",
                    ask="Can I get the member ID for this policy?",
                ),
```

`catalog/disease_only.py` — `policy_number` (lines 164-166):

```python
                prompt=FieldPrompt(
                    confirm="I have the member ID as {{value}} — can you confirm that is correct?",
                    ask="Can I get the member ID for this policy?",
                ),
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py -k "prompt_ask or confirm_leaf" -v
```

Expected: PASS (3 tests).

- [ ] **Step 6: Recompile the schema artifacts**

```bash
just compile-schemas
git diff --stat data/form_schemas/
```

Expected: `ibv_form_standard_v2.json` and `disease_only_verification.json` change (four leaves gain an `ask` key). `ibv_form_standard.json` (legacy v1) must NOT change.

- [ ] **Step 7: Run the full gate**

```bash
just check
```

Expected: PASS. If the prompt snapshot test fails here, **stop** — it should not, because Task 1 does not change rendered text (`_question_lines` reads `prompt.confirm` only). A snapshot diff at this point means an unintended change; investigate before continuing.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py \
        packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
        packages/vera_core/src/vera_core/forms/catalog/disease_only.py \
        data/form_schemas/ tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): require prompt.ask on confirm-role leaves

A confirm leaf reads back a prefilled value. When intake supplied none there
was no authored alternative, so the agent improvised. Every confirm leaf now
carries the open question to ask instead."
```

---

### Task 2: Placeholder validation for leaf prompts

Leaf-level `prompt.ask` / `prompt.confirm` get no placeholder validation today — `dsl.py:652-668` validates task-level text only, which is why a bad token in a field prompt reaches the spoken prompt verbatim. This also scopes `{{value}}` to the one place it is legal.

Verified safe: `{{value}}` is authored in exactly the four confirm prompts from Task 1 and nowhere else in either catalog, and in no task-level text.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py` (document validator, near lines 600-670)
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py:452-489` (`validate_prompt_document`)
- Test: `tests/unit/forms/test_schema_dsl.py`, `tests/unit/forms/test_prompt_document.py`

**Interfaces:**
- Consumes: Task 1's `prompt.ask` on confirm leaves (so the new rules do not reject current catalogs).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/forms/test_schema_dsl.py`. Document validation **raises `ValidationError`** — it does not return an error list — so these follow the file's existing `minimal_doc()` + `pytest.raises` pattern (see `test_ask_field_without_prompt_rejected` at line 179). `minimal_doc()` returns a mutable `dict`; mutate a copy and hand it to `FormSchemaDoc.model_validate`.

Add these to the same class the other document-validation tests live in:

```python
    def test_leaf_prompt_unknown_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = (
            "What is the {{not_a_token}}?"
        )
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_leaf_prompt_malformed_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = (
            "What is the {{ value }}?"
        )
        with pytest.raises(ValidationError, match="malformed placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_value_token_rejected_in_prompt_ask(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["plan_type"]["prompt"]["ask"] = "Is it {{value}}?"
        with pytest.raises(ValidationError, match=r"only valid in a confirm-role"):
            FormSchemaDoc.model_validate(doc)

    def test_value_token_allowed_in_confirm_prompt(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["member_id"] = {
            "type": "text",
            "title": "Policy / Member ID",
            "role": "confirm",
            "prompt": {
                "confirm": "I have the member ID as {{value}} — right?",
                "ask": "Can I get the member ID?",
            },
        }
        FormSchemaDoc.model_validate(doc)
```

In `tests/unit/forms/test_prompt_document.py`, follow that file's existing pattern for building a `PromptDocument` and calling `validate_prompt_document` (which **returns** a list of errors — unlike the schema validator):

```python
def test_value_token_rejected_in_task_override() -> None:
    doc = PromptDocument(
        session=SessionBlock(
            persona="p", goal="g", base_instructions="Confirm {{value}} with the rep."
        ),
        task_overrides={},
    )
    errors = validate_prompt_document(doc, IBV)
    assert any("only valid in a schema field" in e for e in errors)
```

Import `IBV` with `from .test_call_plan import IBV` if the module does not already load a schema doc; match whatever the file already does.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py -k "leaf_prompt or value_token" \
             tests/unit/forms/test_prompt_document.py -k value_token -v
```

Expected: all five FAIL — no errors are produced for any of these inputs today.

- [ ] **Step 3: Add leaf-prompt placeholder validation**

In `dsl.py`'s document validator, inside the existing leaf walk (the `walk_fields` closure that already collects `leaves`, near lines 600-625), add a check per leaf. `context_paths`, `malformed_placeholders`, `PLACEHOLDER_RE` and `RESERVED_PLACEHOLDER_TOKENS` are all already in scope at that point in the method — reuse them exactly as the task-level check at lines 652-668 does.

```python
        def check_leaf_prompt(path: str, leaf: Leaf) -> None:
            for attr in ("ask", "confirm"):
                text = getattr(leaf.prompt, attr, None) if leaf.prompt else None
                if text is None:
                    continue
                for token in PLACEHOLDER_RE.findall(text):
                    if token == "value":
                        if not (attr == "confirm" and leaf.role == "confirm"):
                            errors.append(
                                f"{path}.prompt.{attr}: {{{{value}}}} is only valid in a "
                                "confirm-role leaf's prompt.confirm"
                            )
                        continue
                    if (
                        token not in RESERVED_PLACEHOLDER_TOKENS
                        and token not in (self.system_fields or {})
                        and token not in context_paths
                    ):
                        errors.append(
                            f"{path}.prompt.{attr}: unknown placeholder {{{{{token}}}}} "
                            "(not a system_fields key or context-leaf path)"
                        )
                for snippet in malformed_placeholders(text):
                    errors.append(
                        f"{path}.prompt.{attr}: malformed placeholder {snippet!r} "
                        "(use {{token}} — word characters and dots only, no spaces)"
                    )
```

Call it for every leaf in the same walk that populates `leaves`. If `context_paths` is computed later in the method (it is defined at line 634, after the walk), move the leaf-prompt check into a separate loop placed **after** line 634:

```python
        for path, leaf in leaves.items():
            check_leaf_prompt(path, leaf)
```

- [ ] **Step 4: Reject `{{value}}` in tenant prompt documents**

In `prompting.py`'s `validate_prompt_document` (lines 452-489), `{{value}}` is currently exempt because `RESERVED_PLACEHOLDER_TOKENS` is folded into `valid_tokens` (line 464). Add an explicit rejection at the top of the token loop (line 484, inside `for where, text in texts:` — `where` is the existing location variable):

```python
        for token in PLACEHOLDER_RE.findall(text or ""):
            if token == "value":
                errors.append(
                    f"{where}: {{{{value}}}} is only valid in a schema field's "
                    "prompt.confirm, not in session or task text"
                )
                continue
            if token not in valid_tokens:
                errors.append(f"{where}: unknown placeholder {{{{{token}}}}}")
```

Also update the docstring at lines 458-459 — it currently says reserved tokens `({{value}}, {{current_year}})` are exempt. It must name `{{current_year}}` only.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py tests/unit/forms/test_prompt_document.py -v
```

Expected: PASS, including all pre-existing tests in both files.

- [ ] **Step 6: Run the full gate**

```bash
just check
```

Expected: PASS. A failure naming a real catalog leaf means that leaf has a genuinely bad token — fix the catalog, do not weaken the rule.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py \
        packages/vera_core/src/vera_core/forms/prompting.py \
        tests/unit/forms/test_schema_dsl.py tests/unit/forms/test_prompt_document.py
git commit -m "feat(forms): validate placeholders in leaf prompts

Only task-level text was checked, so a bad token in a field prompt reached
the spoken prompt verbatim. {{value}} is now scoped to a confirm-role leaf's
prompt.confirm, the one place it can be resolved."
```

---

### Task 3: `_render_value` stops rendering `"N/A"`

`ivr_selection._spoken_value` (`services/ivr_selection.py:75-88`) already drops `"N/A"` so an unfilled placeholder never gets spoken; the call-plan fuser never mirrored it. Task 4 depends on this: `"N/A"` must count as *absent* so the slot picks the ask variant.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py:233-242`
- Test: `tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_render_value(raw: Any) -> str | None` now returns `None` for a string that is blank or case-insensitively `"N/A"`. Task 4's slot expansion treats `None` as "nothing on file".

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("raw", ["N/A", "n/a", " N/A ", "", "   "])
def test_render_value_drops_placeholder_strings(raw: str) -> None:
    assert _render_value(raw) is None


def test_render_value_keeps_real_values() -> None:
    assert _render_value("Jane Doe") == "Jane Doe"
    assert _render_value("1991-04-12") == "April 12, 1991"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k render_value -v
```

Expected: `test_render_value_drops_placeholder_strings` FAILS — `_render_value("N/A")` returns `"N/A"`.

- [ ] **Step 3: Implement**

Replace the `isinstance(raw, str)` branch in `_render_value` (`call_plan.py:236-237`):

```python
    if isinstance(raw, str):
        text = raw.strip()
        # Mirrors ivr_selection._spoken_value: "N/A" is the intake default and the
        # inapplicable marker, never a value worth speaking.
        if not text or text.upper() == "N/A":
            return None
        return _speak_iso_date(text)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k render_value -v
```

Expected: PASS.

- [ ] **Step 5: Run the full gate**

```bash
just check
```

Expected: PASS. Some existing `known_information` / `on_file_values` assertions may now legitimately drop an `"N/A"` line — update those expectations; do not revert the behaviour.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py tests/unit/forms/test_call_plan.py
git commit -m "fix(forms): stop rendering \"N/A\" as a spoken prefill value

\"N/A\" is the intake default and the inapplicable marker. Reading it back as
a value is never right; ivr_selection already suppressed it."
```

---

### Task 4: The confirm slot — compiler emits, fuser expands

The atomic core of the fix. These two halves ship together: a compiled slot with no fuser to expand it would put `{{confirm:...}}` on the wire.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py:358-363` (immediate confirms), `:419-425` (end-of-task confirms)
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (module constants, `PrefillFuser.__init__`, `PrefillFuser.fuse`)
- Test: `tests/unit/forms/test_call_plan.py`, `tests/unit/forms/test_prompting.py`
- Regenerate: `tests/unit/forms/snapshots/*.prompt.txt`, `data/form_schemas/` (unchanged by this task, but re-run to confirm)

**Interfaces:**
- Consumes: Task 1 (`leaf.prompt.ask` present on every confirm leaf), Task 3 (`_render_value` returns `None` for `"N/A"`).
- Produces:
  - `CONFIRM_SLOT_RE: re.Pattern[str]` in `call_plan.py`, matching `{{confirm:<path>}}`.
  - Compiled task prompts contain `{{confirm:<root-anchored path>}}` where a confirm sentence used to be.
  - Fused task prompts contain `confirm — <sentence>` or `ask — <sentence>` and never `{{value}}`.

- [ ] **Step 1: Write the failing fuser tests**

In `tests/unit/forms/test_call_plan.py`. The module already provides `IBV` (the loaded `ibv_form_standard_v2.json` doc, line 28), `PLAN` (the compiled template, line 35) and `plan_task(plan, key) -> PlanTask` (line 42) — use those, do not hand-roll a document. Add `focus_call_plan` to the module's imports if absent.

```python
SPOUSE_NAME = "sections.patient_information.spouse_partner_name"
SPOUSE_DOB = "sections.patient_information.spouse_partner_dob"


class TestConfirmSlot:
    def test_expands_to_confirm_when_value_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "Jane Doe"}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "confirm — Can we also check the spouse on the plan?" in text
        assert "I have the spouse listed as Jane Doe" in text
        assert "{{value}}" not in text
        assert "{{confirm:" not in text

    def test_expands_to_ask_when_nothing_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "ask — Can we also check the spouse on the plan?" in text
        assert "Can I get the spouse's full name?" in text
        assert "{{value}}" not in text
        assert "{{confirm:" not in text

    def test_treats_na_as_nothing_on_file(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "N/A"}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "ask — Can we also check the spouse on the plan?" in text

    def test_speaks_iso_date_in_confirm_variant(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_DOB: "1991-04-12"}, current_year=2026)
        assert "April 12, 1991" in plan_task(plan, "insurance_basics").prompt

    def test_focused_retry_still_reads_back_the_value(self) -> None:
        """focus_call_plan clears on_file_values; the value must survive inline."""
        plan = fuse_prefill(IBV, PLAN, {SPOUSE_NAME: "Jane Doe"}, current_year=2026)
        focused = focus_call_plan(plan, [SPOUSE_NAME])
        assert focused.on_file_values is None
        assert "Jane Doe" in plan_task(focused, "insurance_basics").prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k confirm_slot -v
```

Expected: all FAIL — the compiled prompt still carries a baked sentence with `{{value}}`, so `"{{value}}" not in text` fails and no `confirm — ` / `ask — ` prefix exists.

- [ ] **Step 3: Emit the slot from the compiler**

In `prompting.py`, add the helper near `_join_gates` (line 153):

```python
def _confirm_slot(path: str) -> str:
    """The fuse-time slot for a confirm leaf's spoken line — sentence and confirm/ask
    verb are chosen per form, once the prefilled value is known."""
    return f"{{{{confirm:{path}}}}}"
```

Replace the `immediate` block at lines 358-363:

```python
    if immediate:
        lines.append("   - Immediately after this answer:")
        for cpath, _cleaf, cgates in immediate:
            cond_txt = _join_gates(cgates, render_cond)
            lines.append(f"     * If {cond_txt}: {_confirm_slot(cpath)}")
```

Replace the `end_confirms` block at lines 419-425:

```python
    if end_confirms:
        lines = ["Before finishing this task:"]
        for cpath, _leaf, gates in end_confirms:
            only = f" (only if {_join_gates(gates, render_cond)})" if gates else ""
            lines.append(f"- {_confirm_slot(cpath)}{only}")
        blocks.append("\n".join(lines))
```

The header drops ", confirm" because the list can now mix confirms and asks.

`_question_lines` no longer reads `cleaf`; if ruff flags the unused loop variable, keep the `_cleaf` underscore prefix as written above.

- [ ] **Step 4: Expand the slot in the fuser**

In `call_plan.py`, add beside the other module constants (near line 226):

```python
CONFIRM_SLOT_RE = re.compile(r"\{\{confirm:([\w.]+)\}\}")
_VALUE_TOKEN = "{{value}}"
```

In `PrefillFuser.__init__`, after the `_confirm_leaves` assignment (line 279-281), add the sentence map. Keyed on `confirm_in_task` so it mirrors exactly which leaves the compiler emits slots for:

```python
        # Sentences for the {{confirm:path}} slots: the compiler emits one per
        # confirm_in_task leaf and the per-form value decides which is spoken.
        self._confirm_prompts: dict[str, tuple[str, str]] = {}
        for path, leaf in doc.leaf_items():
            if leaf.confirm_in_task is None or leaf.prompt is None:
                continue
            self._confirm_prompts[path] = (
                leaf.prompt.confirm or leaf.title,
                leaf.prompt.ask or leaf.title,
            )
```

In `fuse`, add `expand_slots` next to `hydrate` and count unbacked slots:

```python
        unbacked = 0

        def expand_slots(text: str) -> str:
            def repl(match: re.Match[str]) -> str:
                nonlocal unbacked
                path = match.group(1)
                sentences = self._confirm_prompts.get(path)
                if sentences is None:
                    # Fail safe: an open ask is never wrong, a fabricated read-back is.
                    unbacked += 1
                    return f"ask — {self._titles.get(path, path)}"
                confirm_text, ask_text = sentences
                rendered = _render_value(values.get(path))
                if rendered is None:
                    return f"ask — {ask_text}"
                return f"confirm — {confirm_text.replace(_VALUE_TOKEN, rendered)}"

            return CONFIRM_SLOT_RE.sub(repl, text)
```

Change the task update at lines 341-350 so pass 1 runs before pass 2:

```python
                "tasks": [
                    task.model_copy(
                        update={
                            "intro": hydrate(task.intro),
                            "outro": hydrate(task.outro),
                            "prompt": hydrate(expand_slots(task.prompt)) or "",
                        }
                    )
                    for task in plan.tasks
                ],
```

Extend the existing warning block at lines 356-362 to report unbacked slots — counts only, never content:

```python
        if unbacked:
            logger.warning(
                "call plan %s: %d confirm slot(s) had no sentence; asked openly instead",
                plan.insurance_type,
                unbacked,
            )
```

- [ ] **Step 5: Run the fuser tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k confirm_slot -v
uv run pytest tests/unit/forms/test_call_plan.py -k focused_retry -v
```

Expected: PASS.

- [ ] **Step 6: Re-record the prompt snapshots and read the diff**

```bash
uv run pytest tests/unit/forms/test_prompting.py -k Snapshots -v
```

Expected: FAIL with "stale — see docstring". Re-record with the mechanism `TestSnapshots` documents (`tests/unit/forms/test_prompting.py:155-165`):

```bash
UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/forms/test_prompting.py -k Snapshots
git diff tests/unit/forms/snapshots/
```

Then **read the diff**. Lines 33-35 must change from the baked sentence to slots:

```
   - Immediately after this answer:
     * If "Coverage Type" is "Family": {{confirm:sections.patient_information.spouse_partner_name}}
     * If "Coverage Type" is "Family": {{confirm:sections.patient_information.spouse_partner_dob}}
```

Nothing else in the snapshot should move except the `Before finishing this task:` header wherever an end-of-task confirm block exists. If other lines shift, investigate before accepting.

- [ ] **Step 7: Add a fused snapshot test for both branches**

This is the test that proves the original bug dead — the existing snapshot covers the template only.

Add to the `TestSnapshots` class in `tests/unit/forms/test_prompting.py`, reusing its `_check(name, text)` helper so the same `UPDATE_SNAPSHOTS=1` flow re-records them. Import `IBV`, `PLAN`, `plan_task` and `fuse_prefill` — `test_call_plan` already imports across modules (`from .test_prompting import FORM_SCHEMA_DIR`), so `from .test_call_plan import IBV, PLAN, plan_task` is consistent with the codebase; if that creates a circular import, load the doc locally the same way `test_call_plan.py:28` does.

```python
    def test_fused_insurance_basics_with_spouse_on_file(self) -> None:
        plan = fuse_prefill(
            IBV,
            PLAN,
            {
                "sections.patient_information.spouse_partner_name": "Jane Doe",
                "sections.patient_information.spouse_partner_dob": "1991-04-12",
            },
            current_year=2026,
        )
        self._check(
            "ibv_insurance_basics.fused_with_spouse.prompt.txt",
            plan_task(plan, "insurance_basics").prompt,
        )

    def test_fused_insurance_basics_without_spouse(self) -> None:
        plan = fuse_prefill(IBV, PLAN, {}, current_year=2026)
        text = plan_task(plan, "insurance_basics").prompt
        assert "{{" not in text
        self._check("ibv_insurance_basics.fused_without_spouse.prompt.txt", text)
```

Record them with `UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/forms/test_prompting.py -k Snapshots`, then **read both new files in full** before committing. The without-spouse file is the direct proof: it must contain `ask — Can we also check the spouse on the plan?` and no `{{` anywhere.

- [ ] **Step 8: Run the full gate**

```bash
just compile-schemas && just check
```

Expected: PASS, and `git diff --stat data/form_schemas/` shows no change from this task (the compiled *schema* is unaffected; only rendered prompts changed).

- [ ] **Step 9: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompting.py \
        packages/vera_core/src/vera_core/forms/call_plan.py \
        tests/unit/forms/
git commit -m "fix(forms): resolve confirm read-backs at fuse time

The confirm sentence was frozen at compile time with a {{value}} hole that
nothing ever filled, so with no prefill the agent spoke the literal token or
invented a value. The compiler now emits a per-leaf slot and the fuser picks
the read-back or the authored open ask from the form's own values.

Also fixes focused retries, which clear on_file_values and so guaranteed an
unbacked token."
```

---

### Task 5: Three-state gating

`_apply_gating` reports any currently-false gate as excluded, including one whose own input is unanswered — so at `insurance_basics` entry the spouse fields are listed under "do NOT ask these" while the task prompt says to confirm them if Family.

No new condition helper is needed: `dsl.condition_field_paths(cond, shared, depth=0) -> Iterator[str]` (`dsl.py:106-122`) already yields every referenced path with shared refs expanded and a recursion guard. `conditions.py` is **not** modified, so no frontend mirror is required.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py` (new public `render_gate_chains`)
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (`PlanFieldDescriptor.gate_text`, populate in `compile_call_plan`)
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:118-140, 231-250, 252-270, 787-799`
- Test: `tests/unit/forms/test_call_plan.py`, `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: Task 4's `SPOUSE_NAME` constant in `tests/unit/forms/test_call_plan.py` (define it there if Task 4 was skipped). No production code from Tasks 1-4.
- Produces:
  - `prompting.render_gate_chains(doc: FormSchemaDoc) -> dict[str, str]` — rendered gate prose per gated leaf path.
  - `PlanFieldDescriptor.gate_text: str | None` — the condition prose alone (e.g. `'"Coverage Type" is "Family"'`), `None` when ungated.
  - `PlanRunController.excluded_fields(task_index) -> list[PlanFieldDescriptor]` and `.conditional_fields(task_index) -> list[PlanFieldDescriptor]`, **replacing** `inapplicable_fields`.

- [ ] **Step 1: Write the failing `gate_text` test**

In `tests/unit/forms/test_call_plan.py`, using the module-level `PLAN`:

```python
def test_gate_text_carries_the_condition_prose() -> None:
    fields = {f.path: f for t in PLAN.tasks for f in t.fields}
    assert fields[SPOUSE_NAME].gate_text == '"Coverage Type" is "Family"'
    assert fields["sections.benefit_coverage.coverage_type"].gate_text is None
```

The expected string is not a guess — it is exactly what the committed snapshot `tests/unit/forms/snapshots/ibv_insurance_basics.prompt.txt:34` renders for this same gate chain (`* If "Coverage Type" is "Family": …`), which is the point of reusing the compiler's renderer.

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k gate_text -v
```

Expected: FAIL with `AttributeError` / unknown field `gate_text`.

- [ ] **Step 3: Add `render_gate_chains` and `gate_text`**

In `prompting.py`, add after `_join_gates` (line 162):

```python
def render_gate_chains(doc: FormSchemaDoc) -> dict[str, str]:
    """Rendered gate-chain prose per gated leaf path — the same words the compiled
    task prompt uses, so a runtime consumer states a condition identically."""
    render_cond = build_condition_renderer(doc)
    return {
        path: _join_gates(gates, render_cond)
        for path, _leaf, gates in leaf_gates(doc)
        if gates
    }
```

In `call_plan.py`, add the field to `PlanFieldDescriptor` after `gates` (line 82):

```python
    gate_text: str | None = None
```

In `compile_call_plan`, build the map once before the `leaf_gates` loop and pass it in:

```python
    gate_texts = render_gate_chains(doc)
```

```python
                gates=gates,
                gate_text=gate_texts.get(path),
```

Add `render_gate_chains` to the existing `from vera_core.forms.prompting import ...` in `call_plan.py`.

- [ ] **Step 4: Run it to verify it passes**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -k gate_text -v
```

Expected: PASS.

- [ ] **Step 5: Write the failing three-state tests**

In `apps/agent_worker/tests/unit/test_plan_runtime.py`, using the module's existing controller-building helper:

```python
_COVERAGE = "sections.benefit_coverage.coverage_type"
_SPOUSE_NAME = "sections.patient_information.spouse_partner_name"


def test_unanswered_gate_is_conditional_not_excluded() -> None:
    controller = _controller()
    controller.update_answers({})
    idx = _task_index(controller, "insurance_basics")
    assert _SPOUSE_NAME in {f.path for f in controller.conditional_fields(idx)}
    assert _SPOUSE_NAME not in {f.path for f in controller.excluded_fields(idx)}


def test_answered_false_gate_is_excluded() -> None:
    controller = _controller()
    controller.update_answers({_COVERAGE: "Individual"})
    idx = _task_index(controller, "insurance_basics")
    assert _SPOUSE_NAME in {f.path for f in controller.excluded_fields(idx)}
    assert _SPOUSE_NAME not in {f.path for f in controller.conditional_fields(idx)}


def test_answered_true_gate_is_applicable() -> None:
    controller = _controller()
    controller.update_answers({_COVERAGE: "Family"})
    idx = _task_index(controller, "insurance_basics")
    assert _SPOUSE_NAME in {f.path for f in controller.applicable_fields(idx)}


def test_gating_block_lists_conditional_fields_with_their_condition() -> None:
    block = _gating_block(
        applicable=[],
        excluded=[],
        conditional=[
            PlanFieldDescriptor(
                path=_SPOUSE_NAME,
                title="Spouse / Partner Name",
                type="text",
                role="confirm",
                gate_text='"Coverage Type" is "Family"',
            )
        ],
    )
    assert "# Conditional on this call" in block
    assert 'Spouse / Partner Name — only if "Coverage Type" is "Family"' in block
    assert "do NOT ask these" not in block


def test_conditional_field_is_never_also_excluded() -> None:
    """The three buckets partition the task's fields — no field in two, none dropped."""
    controller = _controller()
    controller.update_answers({_COVERAGE: "Family"})
    idx = _task_index(controller, "insurance_basics")
    buckets = [
        {f.path for f in controller.applicable_fields(idx)},
        {f.path for f in controller.excluded_fields(idx)},
        {f.path for f in controller.conditional_fields(idx)},
    ]
    assert buckets[0] & buckets[1] == set()
    assert buckets[0] & buckets[2] == set()
    assert buckets[1] & buckets[2] == set()
    assert set().union(*buckets) == {f.path for f in controller.plan.tasks[idx].fields}
```

`_controller()` and `_task_index()` are stand-ins for whatever this module already uses to build a `PlanRunController` and locate a task — read `apps/agent_worker/tests/unit/test_plan_runtime.py` and reuse its existing setup rather than adding new helpers. If a task index is needed, `next(i for i, t in enumerate(controller.plan.tasks) if t.task_key == "insurance_basics")` needs no helper at all.

- [ ] **Step 6: Run them to verify they fail**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -k "conditional or excluded or gating_block" -v
```

Expected: FAIL with `AttributeError: 'PlanRunController' object has no attribute 'conditional_fields'` and a `_gating_block()` arity error.

- [ ] **Step 7: Implement the three-state partition**

In `plan_runtime.py`, add `evaluate` to the `vera_core.forms.conditions` import and `condition_field_paths` to the `vera_core.forms.dsl` import.

Replace `inapplicable_fields` (lines 787-799) with:

```python
    def excluded_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """Questions a gate rules out decidably — some gate is false with every path it
        reads already answered, so no later answer can turn it back on."""
        return [
            field
            for field in self._ungated_out(task_index)
            if self._has_decided_false_gate(field)
        ]

    def conditional_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """Questions whose gates are not yet decidable — a referenced answer is still
        missing, so the compiled prompt's own condition governs them."""
        return [
            field
            for field in self._ungated_out(task_index)
            if not self._has_decided_false_gate(field)
        ]

    def _ungated_out(self, task_index: int) -> list[PlanFieldDescriptor]:
        shared = self.plan.shared_conditions
        return [
            field
            for field in self.plan.tasks[task_index].fields
            if not is_applicable(field.gates, self._answers, shared)
        ]

    def _has_decided_false_gate(self, field: PlanFieldDescriptor) -> bool:
        """`is_applicable` is `all(gates)`, so ONE decidably-false gate settles the
        whole chain — regardless of other gates reading unanswered paths."""
        shared = self.plan.shared_conditions
        return any(
            not evaluate(gate, self._answers, shared)
            and all(self._is_answered(ref) for ref in condition_field_paths(gate, shared))
            for gate in field.gates
        )
```

Replace `_gating_block` (lines 118-140):

```python
def _gating_block(
    applicable: list[PlanFieldDescriptor],
    excluded: list[PlanFieldDescriptor],
    conditional: list[PlanFieldDescriptor],
) -> str:
    """This call's narrowed question list, or "" when the gates settle nothing.

    Leads with what DOES apply. An exclusions-only list was read as "the whole task is
    excluded" — the financial task announced itself and completed in the same turn, claiming
    every question was gated out while its deductible fields were still open. A positive
    enumeration cannot be over-generalized into an empty task.

    A gate whose own input is unanswered is CONDITIONAL, never excluded: reporting it as
    excluded contradicted the task prompt's own condition and the agent improvised.
    """
    if not excluded and not conditional:
        return ""
    sections: list[str] = []
    if applicable:
        sections.append(
            "# Questions that apply on THIS call — ask every one of them\n"
            f"{_field_lines(applicable)}"
        )
    if conditional:
        sections.append(
            "# Conditional on this call — ask only if the condition holds\n"
            f"{_conditional_lines(conditional)}"
        )
    if excluded:
        sections.append(
            "# Excluded by the plan's gates — do NOT ask these, whatever the task list says\n"
            f"{_field_lines(excluded)}"
        )
    return "\n\n".join(sections)


def _conditional_lines(fields: list[PlanFieldDescriptor]) -> str:
    return "\n".join(
        f"- {field.title} — only if {field.gate_text}" if field.gate_text else f"- {field.title}"
        for field in fields
    )
```

Update `_apply_gating` (lines 264-267):

```python
        gating = _gating_block(
            self._controller.applicable_fields(self._task_index),
            self._controller.excluded_fields(self._task_index),
            self._controller.conditional_fields(self._task_index),
        )
```

Update `_skip_when_nothing_applies` (line 238) so an undecided task still runs:

```python
        if (
            not self._task.fields
            or self._controller.applicable_fields(self._task_index)
            or self._controller.conditional_fields(self._task_index)
        ):
            return False
```

Leave `gap_fields` (lines 801-810) alone — it must keep counting only genuinely applicable fields.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: PASS. Any pre-existing test calling `inapplicable_fields` must be updated to the new methods.

- [ ] **Step 9: Run the full gate**

```bash
just check
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompting.py \
        packages/vera_core/src/vera_core/forms/call_plan.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        tests/unit/forms/test_call_plan.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(agent): stop reporting undecided gates as excluded

A missing answer compares as \"\", so a gate whose own question had not been
asked yet was reported to the agent as excluded — the spouse fields landed
under \"do NOT ask these\" while the task prompt said to confirm them if the
coverage was Family. Undecided gates are now their own state."
```

---

### Task 6: Reject out-of-enum values at intake

There is no enum membership check anywhere in the intake path today: `intake.py` never reads `leaf.values`, and the pipeline validates unknown paths → 422, phone prefix → normalised, dates → parsed or 422, and nothing else. Any out-of-enum string lands in a `field_answer` row as a legal-looking answer, and under Task 5 an out-of-enum value counts as *answered* — making the gate decidably false and hard-excluding the questions behind it.

**This is defense in depth, not a fix for a known live instance.** The AD19 dropdown is confirmed correct (see Task 7), so there is no reproducer today. It earns its place because Sheets validation can be configured to warn rather than reject, AD19 is one of many enum leaves, and a schema that declares `values` and then accepts anything is a validator gap.

**Careful:** many enum leaves declare `default="N/A"` (e.g. `pcp_referral_required`) and gated leaves declare `inapplicable_value`. Both must count as accepted, or this task 422s legitimate intakes.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/intake.py` (add after `unknown_payload_paths`, line 269)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (helper near line 162, call site near line 235)
- Test: `tests/unit/forms/test_intake.py`

**Interfaces:**
- Consumes: nothing. (Independent of Task 7 — the earlier draft ordered these two; that dependency is gone.)
- Produces: `intake.validate_enum_answers(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> None`, raising `InvalidIntakeValue(path, reason)`.

- [ ] **Step 1: Write the failing tests**

`test_intake.py` has no full v2 doc today (its `SCHEMA` constant is a v1 stand-in), so import the real one: `from .test_call_plan import IBV`. Add `validate_enum_answers` to the existing `from vera_core.forms.intake import (...)` block.

```python
COVERAGE_TYPE = "sections.benefit_coverage.coverage_type"


def test_enum_answer_outside_declared_values_is_rejected() -> None:
    with pytest.raises(InvalidIntakeValue) as exc:
        validate_enum_answers([(COVERAGE_TYPE, "PT/Spouse")], IBV)
    assert exc.value.field_path == COVERAGE_TYPE
    assert "PT/Spouse" not in str(exc.value)


def test_declared_enum_value_is_accepted() -> None:
    validate_enum_answers([(COVERAGE_TYPE, "Family")], IBV)


def test_enum_default_is_accepted() -> None:
    """pcp_referral_required declares default="N/A", so intake may send it."""
    validate_enum_answers([("sections.benefit_coverage.pcp_referral_required", "N/A")], IBV)


def test_blank_enum_answer_passes_through() -> None:
    validate_enum_answers([(COVERAGE_TYPE, "")], IBV)


def test_non_enum_answer_is_ignored() -> None:
    validate_enum_answers([("sections.insurance_information.group_name", "Anything")], IBV)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/unit/forms/test_intake.py -k enum -v
```

Expected: FAIL with `ImportError` / `NameError` for `validate_enum_answers`.

- [ ] **Step 3: Implement in `intake.py`**

Add after `unknown_payload_paths` (line 269):

```python
def enum_accepted_values(doc: FormSchemaDoc) -> dict[str, set[str]]:
    """Accepted intake answers per enum leaf — declared `values` plus `special_values`,
    the leaf's own `default` and its `inapplicable_value`."""
    accepted: dict[str, set[str]] = {}
    for path, leaf in doc.leaf_items():
        if leaf.type != "enum" or not leaf.values:
            continue
        allowed = set(leaf.values) | set(leaf.special_values or [])
        if leaf.default is not None:
            allowed.add(str(leaf.default))
        if leaf.inapplicable_value is not None:
            allowed.add(leaf.inapplicable_value)
        accepted[path] = allowed
    return accepted


def validate_enum_answers(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> None:
    """Reject an enum leaf's intake value that is not one of its accepted options.

    Blank values pass through (a caller may clear a field). Raises `InvalidIntakeValue`
    carrying the offending path only — never the value (PHI)."""
    accepted = enum_accepted_values(doc)
    for path, raw in answers:
        allowed = accepted.get(path)
        if allowed is None:
            continue
        text = _clean_str(raw)
        if text is not None and text not in allowed:
            raise InvalidIntakeValue(path, "value is not one of the field's declared options")
```

- [ ] **Step 4: Run them to verify they pass**

```bash
uv run pytest tests/unit/forms/test_intake.py -k enum -v
```

Expected: PASS.

- [ ] **Step 5: Wire the 422**

In `patient_forms.py`, add beside `_normalize_date_answers_or_422` (line 162):

```python
def _validate_enum_answers_or_422(answers: list[tuple[str, Any]], doc: FormSchemaDoc) -> None:
    """`validate_enum_answers`, translated to the API's validation-error contract."""
    try:
        validate_enum_answers(answers, doc)
    except InvalidIntakeValue as exc:
        _raise_422(exc)
```

Add the call after `_normalize_date_answers_or_422` (line 235), before `_promote_or_422`:

```python
        _validate_enum_answers_or_422(answers, doc)
```

Add `validate_enum_answers` to the existing `from vera_core.forms.intake import ...`.

- [ ] **Step 6: Run the full gate**

```bash
just check
```

Expected: PASS. If an existing intake test fixture uses an out-of-enum value, fix the fixture — that fixture was asserting the bug.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/intake.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/unit/forms/test_intake.py
git commit -m "feat(intake): reject enum answers outside a leaf's declared options

Nothing validated enum membership, so any out-of-enum string was stored as a
legal answer and silently poisoned every gate reading it. Defense in depth:
no reproducer today, but a declared values list should be authoritative.
Paths only in the error — never the value."
```

---

### Task 7: Revive the dead spouse-required guard in the sheet

**No vocabulary mapping is needed.** A screenshot of the live sheet confirms AD19's dropdown offers exactly `Individual` and `Family` — precisely the leaf's declared `values`.

The real defect: the spouse-required block at `ibv_infertility_appscript.js:619-641` is gated on `AD19.toLowerCase() === "pt/spouse"`. That string occurs **once** in the entire file, and AD19 can only ever hold `Individual` or `Family`, so the comparison is never true and the whole block is dead code. The one check meant to stop a Family form reaching a call with empty spouse cells has never run — which is how the form in the reported transcript was created.

This **narrows** the hole rather than closing it: AD19 is optional (`coverage_type: ["AD19", false]`, line 38), so a clinic that does not yet know the coverage type still submits blank and the rep may still say "Family" on the call. Task 4 is what covers that path and remains the primary fix.

**Files:**
- Modify: `data/ibv_infertility_appscript.js:620-621`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by other tasks.

- [ ] **Step 1: Fix the comparison**

Replace lines 620-621:

```js
  // AD19's dropdown is Individual | Family. This read compares the raw cell, so it
  // must use the sheet's own wording — it read "pt/spouse" and never fired.
  const cellValue = sheet.getRange("AD19").getValue().toString().trim();
  if (cellValue.toLowerCase() === "family") {
```

Change nothing else in the block — the alert and `throw` at lines 636-640 already behave correctly once reached.

- [ ] **Step 2: Verify both branches by hand**

There is no test harness for Apps Script, so exercise it in the sheet directly.

1. Set AD19 = `Family`, clear J12/J13/J14, run the submit action. Expected: the alert lists all three missing spouse cells and submission is blocked.
2. Fill J12/J13/J14, submit again. Expected: submission proceeds.
3. Set AD19 = `Individual`, clear J12/J13/J14, submit. Expected: submission proceeds with no alert.
4. Clear AD19 entirely, submit. Expected: submission proceeds with no alert (the coverage type is unknown at intake — this is the case Task 4 covers on the call).

- [ ] **Step 3: Confirm the stored answer is unchanged**

The mapping is untouched, so `coverage_type` should already store the dropdown string verbatim. From case 2 above, confirm the created form's `coverage_type` answer is exactly `"Family"` and that Task 6's new 422 did not fire.

```bash
just up && just migrate && just api
```

Query the branch's own dev database — this repo uses per-branch DBs, so confirm which one this branch points at before querying rather than assuming `vera`.

- [ ] **Step 4: Commit**

```bash
git add data/ibv_infertility_appscript.js
git commit -m "fix(intake): revive the dead spouse-required check

The block was gated on AD19 == \"pt/spouse\", but that dropdown only ever
holds Individual or Family — so the check never ran and a Family form could
be submitted with the spouse cells empty."
```

---

### Task 8: Simplify, then verify end to end

`just check` gates the merge; a live call gates the ship. A change to spoken output is **not** verified by pytest — the assertions are on strings and the defect lives in the audio.

**Files:** none created; this task may edit any file touched in Tasks 1-7.

- [ ] **Step 1: Run the code-simplifier**

Per the repo-wide rule, trigger it with exactly: **"simplify code"** (launches `code-simplifier` from `code-simplifier@claude-plugins-official`). It must run in the same session as the implementation. Behaviour must not change.

- [ ] **Step 2: Re-run the full gate after simplification**

```bash
just compile-schemas && just check
```

Expected: PASS, and `git status` clean apart from intended edits.

- [ ] **Step 3: Publish the new schema version**

```bash
just seed-schemas
```

Expected: a new `schema_version` is published (idempotent; the equality check is order-sensitive).

- [ ] **Step 4: Listen to the spoken strings**

```bash
uv run --no-project --with certifi python scripts/tts_probe.py --set verbatim
```

Judge the ask variants in their realistic carrier sentence, not in isolation. If any wording reads badly aloud, change the catalog `ask=` text, re-run `just compile-schemas && just check`, and probe again. This is the sign-off on Task 1's wording.

- [ ] **Step 5: Run the eval scenario**

Add a scenario with `coverage_type` answered `"Family"` on the call and **no** spouse prefill, asserting the transcript contains no `{{` and no invented spouse name.

```bash
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

`-m evals` is required — without it you get the LLM-free tests and no simulations, which looks like a clean pass. Confirm real execution by the `===== <scenario>: … =====` banners. Never add these to `just check`. Do not overclaim: a scenario reporting `0 answers extracted` proves nothing.

- [ ] **Step 6: Make a live call**

Run a real IBV call on a form with **blank** spouse name/DOB, and have the rep answer "Family". Confirm by listening:

1. The bot asks openly for the spouse's name and date of birth.
2. It never says "value", "curly", or a bracketed token.
3. It never states a spouse name that was not on the form.
4. On a second call with coverage "Individual", the spouse is never mentioned.

- [ ] **Step 7: Push the branch**

```bash
git push origin HEAD:refs/heads/fix/spouse-name-empty-issue
```

Open the PR from the URL git prints — `origin` is Bitbucket, so `gh` cannot create it. In the PR body, state which verification steps actually ran, including the live-call result.

---

## Follow-ups (not in this plan)

- **Live re-gating (G2).** Re-run `_apply_gating` from `update_answers` (`plan_runtime.py:703`) when the partition changes, so a field moves from conditional to excluded the moment the rep says "Individual". Needs an async hop out of a sync controller method plus change-detection to avoid rewriting instructions every extraction tick; deserves its own live-call verification.
- **`default="N/A"` inflates completion percentage.** `review.py:155` counts a leaf with a declared `default` as filled, so an unfilled spouse name still reads as complete. Same root cause, different consumer.
