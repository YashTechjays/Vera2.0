# Contextual Missing-Question List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `task_complete` refusal and the gap-pass system instruction name each still-missing question by its service, codes and gate — rendered from the compiled question tree instead of from bare storage-field titles.

**Architecture:** One narrowing primitive over the existing `PromptPanel` tree (`keep_questions`, the complement of `drop_questions`), one tree×descriptor join that stamps per-member context and optionally pulls in gate-dependent follow-ups (`focus_questions`), and one compact renderer beside the existing full one (`render_digest`). The three worker call sites stop rendering `PlanFieldDescriptor.title` and start rendering a narrowed copy of the tree. `render_panels`' output for the compiled prompt is unchanged.

**Tech Stack:** Python 3.12, pydantic v2, pytest, ruff, mypy `--strict`, `just` recipes. Pure/DB-free code in `packages/vera_core/src/vera_core/forms/`; the LiveKit worker in `apps/agent_worker/`.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-12-contextual-missing-question-list-design.md`

## Global Constraints

- All work is under `vera-backend/`. Run every command from that directory.
- **Scope is the two FRESH-CALL sites only.** Do NOT touch `focus_call_plan`, `bookend_paths`, or `queue_dispatcher.py` — the focused-retry fix is a separate branch.
- Everything added to `vera_core/forms/` must stay **pure and DB-free** — no DB access, no I/O, deterministic (same inputs → identical output). The agent worker has no `FormSchemaDoc` at runtime.
- **`render_panels`' output for a compiled prompt must not change.** `tests/unit/forms/test_prompting.py::TestPanelsMatchThePrompt` pins it byte-for-byte.
- Code style: PEP 695 type params (`def f[T]`), never `Generic[T]`/`TypeVar`. mypy runs `--strict`.
- Comments only where they explain something the code cannot (a constraint, a trade-off, a non-obvious rule); one line; docstrings one sentence. Never narrate what the code says. This is a repo-wide `CLAUDE.md` rule.
- **PHI:** the rendered block carries hydrated `{{token}}` intake values. Never log it, never log a field value. Log counts and task keys only, exactly as the current code does.
- `condition_field_paths(cond, shared, depth=0)` is typed `shared: dict[str, Condition] | None`. Any function forwarding to it must type its own parameter `dict[str, Condition]`, not `Mapping`, or mypy `--strict` fails.
- Verification gate after each task: `just check` (= `ruff format --check` + `ruff check` + `mypy --strict` + `pytest`). Run it **verbatim** — never a hand-picked subset.
- Neither new model field reaches a stored artifact: `data/form_schemas/*.json` holds the compiled `FormSchemaDoc` (no `PromptQuestion`), and `prompt_version.composite_json` holds a `PromptDocument` (no panels). So **no** `just compile-schemas` or reseed is needed. `CallPlan` *is* staged to Redis per call, and every model there sets `extra="forbid"` — a plan staged before the deploy still validates (both fields have defaults), but a plan staged *after* it would fail an old reader, so control plane and worker deploy together as usual.
- In `tests/unit/forms/test_call_plan.py` reuse the module-level `IBV` doc, the `PLAN` fixture and the `plan_task(plan, key)` helper (lines 37-52) instead of recompiling; `build_ibv_standard` is imported there from `vera_core.forms.catalog.ibv_standard`, and `build_disease_only` lives at `vera_core.forms.catalog.disease_only`.
- Integration tests need a reachable Postgres. This worktree's database is `task_complete_prompt_fix`, and `tests/integration/conftest.py` reads only the process environment (`Settings(_env_file=None)`), so run the gate as:
  ```bash
  VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
  ```
  Baseline on the current tree is **2257 passed, 21 deselected, 1 xfailed**. The conftest creates `task_complete_prompt_fix_test` on demand; leave it in place between tasks.
- Commit after each task. **Never** add a `Co-Authored-By` trailer.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `packages/vera_core/src/vera_core/forms/question_plan.py` | The spoken-question tree and its models | Add `still_needed` to `PromptQuestion`; add `keep_questions`; delete `owed_questions` |
| `packages/vera_core/src/vera_core/forms/prompting.py` | Renders the tree to prompt text | Render `still_needed` in `_numbered_question`; add `render_digest` |
| `packages/vera_core/src/vera_core/forms/call_plan.py` | Runtime projection + tree×descriptor joins | Add `owner_title` to `PlanFieldDescriptor`, compile it; add `focus_questions` |
| `apps/agent_worker/src/agent_worker/plan_runtime.py` | The agent chain and its guards | Rewire 3 call sites; add `PlanRunController.answers` + `gap_panels`; delete `_field_line`/`_field_lines`/`_owning_segment` |
| `tests/unit/forms/test_question_plan.py` | | `TestOwedQuestions` → `TestKeepQuestions` |
| `tests/unit/forms/test_prompting.py` | | `TestRenderDigest`, `still_needed` rendering |
| `tests/unit/forms/test_call_plan.py` | | `owner_title` compilation, `focus_questions` |
| `apps/agent_worker/tests/unit/test_plan_runtime.py` | | `TestFieldLines` → refusal/gap-block tests; update `_INTAKE_GAPS` |

---

### Task 1: `keep_questions` — narrow the tree to a path set

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/question_plan.py` (add `keep_questions` after `drop_questions`, which ends at line 678; delete `owed_questions` at lines 681-689)
- Test: `tests/unit/forms/test_question_plan.py` (replace `TestOwedQuestions`, lines 332-369)

**Interfaces:**
- Consumes: nothing.
- Produces: `keep_questions(panels: list[PromptPanel], wanted: Collection[str]) -> list[PromptPanel]`

**Context you need:** `drop_questions` (same file, lines 633-678) is the mirror image of this function — read it first. It documents two rules this function must also obey: a `confirm_immediate` node's anchor is **positional** (it is whatever question precedes it in the same `items` list), so a confirm run must travel with its anchor; and a routing question (`routes_between` set, `target_paths` empty) collects nothing.

`owed_questions` is being deleted. Its docstring and its test class say it was deliberately kept as "a standalone path-set query"; `keep_questions` subsumes it, and leaving two near-identical narrowing functions side by side is what the deletion prevents. It has **no production caller** — verify with `rg -n 'owed_questions' --type py` before deleting; only its own definition, the `__init__` export list if present, and the test class should appear.

- [ ] **Step 1: Write the failing tests**

Replace the whole `class TestOwedQuestions` block (lines 332-369) with this. Keep the `_OWED_TREE` fixture above it exactly as it is.

```python
class TestKeepQuestions:
    """The complement of `drop_questions`: keep the questions a path set still owes, drop the
    rest, and prune any panel left with nothing to ask."""

    def test_a_multi_target_question_is_kept_once_however_many_targets_are_open(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered", "a.copay", "a.coins"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?"
        ]

    def test_one_open_target_is_enough_to_keep_the_whole_question(self) -> None:
        # The mirror of drop_questions, which keeps a question with even one askable target.
        kept = keep_questions(_OWED_TREE, {"a.copay"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?"
        ]

    def test_nested_panels_are_reached_and_kept(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered", "a.prior_auth"})
        assert [q.text for q in iter_questions(kept)] == [
            "Covered, and what copay and coinsurance?",
            "Is prior authorization required?",
        ]
        assert [p.title for p in kept[0].children] == ["Prior auth"]

    def test_a_panel_with_nothing_owed_is_pruned(self) -> None:
        kept = keep_questions(_OWED_TREE, {"a.covered"})
        assert kept[0].children == []

    def test_nothing_open_keeps_nothing(self) -> None:
        assert keep_questions(_OWED_TREE, set()) == []

    def test_a_confirm_node_travels_with_its_anchor(self) -> None:
        # The anchor is positional, not modeled: keeping the confirm without the question in
        # front of it would re-anchor the bullet onto whatever lands there next.
        tree = [
            PromptPanel(
                title="Basics",
                items=[
                    PromptQuestion(
                        text="Spouse name?", options=[PromptOption(target_paths=["a.spouse"])]
                    ),
                    PromptQuestion(
                        text="Read back the DOB",
                        options=[PromptOption(target_paths=["a.dob"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        assert [q.text for q in iter_questions(keep_questions(tree, {"a.spouse", "a.dob"}))] == [
            "Spouse name?",
            "Read back the DOB",
        ]
        # The anchor alone keeps only itself; the confirm alone keeps neither.
        assert [q.text for q in iter_questions(keep_questions(tree, {"a.spouse"}))] == [
            "Spouse name?"
        ]
        assert keep_questions(tree, {"a.dob"}) == []

    def test_a_routing_question_survives_only_while_two_branches_do(self) -> None:
        # It collects nothing, so it can never itself be owed; it earns its place only while
        # there is still a choice to make. With one branch left its own text would name a
        # panel that is no longer below it.
        def tree() -> list[PromptPanel]:
            return [
                PromptPanel(
                    title="Egg cryo",
                    items=[
                        PromptQuestion(text="Elective or cancer?", routes_between=["Elec", "Canc"]),
                        PromptPanel(
                            title="Elec",
                            items=[
                                PromptQuestion(
                                    text="Elective covered?",
                                    options=[PromptOption(target_paths=["a.elec"])],
                                )
                            ],
                        ),
                        PromptPanel(
                            title="Canc",
                            items=[
                                PromptQuestion(
                                    text="Cancer covered?",
                                    options=[PromptOption(target_paths=["a.canc"])],
                                )
                            ],
                        ),
                    ],
                )
            ]

        both = keep_questions(tree(), {"a.elec", "a.canc"})
        assert [q.text for q in iter_questions(both)][0] == "Elective or cancer?"
        one = keep_questions(tree(), {"a.elec"})
        assert [q.text for q in iter_questions(one)] == ["Elective covered?"]
```

In that file's `vera_core.forms.question_plan` import block (lines 15-25) add `keep_questions` and remove `owed_questions`. `iter_questions`, `PromptOption`, `PromptPanel` and `PromptQuestion` are already imported.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_question_plan.py::TestKeepQuestions -v
```

Expected: collection error — `ImportError: cannot import name 'keep_questions'`.

- [ ] **Step 3: Implement `keep_questions` and delete `owed_questions`**

In `question_plan.py`, delete the whole `owed_questions` function (lines 681-689) and add this immediately after `drop_questions`:

```python
def keep_questions(panels: list[PromptPanel], wanted: Collection[str]) -> list[PromptPanel]:
    """The tree with ONLY the questions that answer a path in `wanted`, panels with nothing
    left pruned.

    The complement of `drop_questions`, and it inherits both of that function's structural
    rules. A confirm node's anchor is positional, so a confirm run travels with the question in
    front of it and never survives alone. A routing question collects nothing and so can never
    be wanted; it is kept only while at least TWO of the panels it routes between survive,
    because with one branch left its rendered text names a panel that is no longer below it.
    """
    kept = set(wanted)
    out: list[PromptPanel] = []
    for panel in panels:
        source = list(panel.items)
        items: list[PromptItem] = []
        i = 0
        while i < len(source):
            item = source[i]
            if isinstance(item, PromptPanel):
                items.extend(keep_questions([item], kept))
                i += 1
                continue
            run: list[PromptQuestion] = []
            j = i + 1
            while j < len(source):
                candidate = source[j]
                if not (isinstance(candidate, PromptQuestion) and candidate.is_confirm):
                    break
                run.append(candidate)
                j += 1
            if item.routes_between:
                items.append(item)  # provisional; pruned below, once the siblings are known
            elif not kept.isdisjoint(item.target_paths):
                items.append(item)
                items.extend(node for node in run if not kept.isdisjoint(node.target_paths))
            i = j
        surviving = {child.title for child in items if isinstance(child, PromptPanel)}
        items = [
            item
            for item in items
            if not (isinstance(item, PromptQuestion) and item.routes_between)
            or len(surviving.intersection(item.routes_between)) >= 2
        ]
        if items:
            out.append(panel.model_copy(update={"items": items}))
    return out
```

`PromptItem` is already defined in this file (line 114) and `Collection` is already imported from `collections.abc` (line 22) — check both rather than re-adding.

If `owed_questions` is re-exported from `packages/vera_core/src/vera_core/forms/__init__.py`, replace that export with `keep_questions`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_question_plan.py -v
```

Expected: PASS, including the pre-existing classes in that file.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

Expected: all four steps green.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/question_plan.py tests/unit/forms/test_question_plan.py
git commit -m "feat(forms): keep_questions narrows the question tree to a path set"
```

---

### Task 2: `still_needed` — name the members a partial fan-out still owes

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/question_plan.py` (add a field to `PromptQuestion`, lines 63-89)
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py` (`_numbered_question`, lines 432-463)
- Test: `tests/unit/forms/test_prompting.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `PromptQuestion.still_needed: list[str]` (default empty), rendered by `_numbered_question` as `   - Still needed for: <a>, <b>.`

**Context you need:** an `AskGroup` compiles to ONE `PromptQuestion` whose `target_paths` are all its members, so a question is atomic — there is no per-member sentence in the tree. When only some members are owed, this annotation is the only way to say which. The compiler never sets it, so a compiled prompt renders byte-identically; only Task 5's narrowing stamps it. That is what keeps `TestPanelsMatchThePrompt` green.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/forms/test_prompting.py`:

```python
class TestStillNeeded:
    """A partially-answered fan-out is one question with some members already on file."""

    def test_still_needed_is_rendered_under_the_question(self) -> None:
        panels = [
            PromptPanel(
                title="Labs",
                items=[
                    PromptQuestion(
                        text="Are codes 58340, 82670 covered?",
                        options=[
                            PromptOption(
                                answers="Yes | No",
                                target_paths=["a.cpt_58340.covered", "a.cpt_82670.covered"],
                            )
                        ],
                        still_needed=["CPT 58340"],
                    )
                ],
            )
        ]
        rendered = render_panels(panels)
        assert "1. Are codes 58340, 82670 covered?" in rendered
        assert "   - Still needed for: CPT 58340." in rendered

    def test_an_unstamped_question_renders_no_such_line(self) -> None:
        panels = [
            PromptPanel(
                title="Labs",
                items=[
                    PromptQuestion(
                        text="Are codes 58340, 82670 covered?",
                        options=[PromptOption(target_paths=["a.cpt_58340.covered"])],
                    )
                ],
            )
        ]
        assert "Still needed" not in render_panels(panels)

    def test_still_needed_does_not_take_an_ordinal(self) -> None:
        panels = [
            PromptPanel(
                items=[
                    PromptQuestion(
                        text="Q",
                        options=[PromptOption(target_paths=["a.x", "a.y"])],
                        still_needed=["CPT 1", "CPT 2"],
                    )
                ]
            )
        ]
        assert numbered_questions(panels) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_prompting.py::TestStillNeeded -v
```

Expected: FAIL — `PromptQuestion` forbids extra fields (`model_config = ConfigDict(extra="forbid")`), so pydantic raises `ValidationError: Extra inputs are not permitted [type=extra_forbidden]` for `still_needed`.

- [ ] **Step 3: Add the field and render it**

In `question_plan.py`, inside `class PromptQuestion`, directly under the `fanned_codes` field (line 75):

```python
    # Which of a fan-out's members are still owed, when only some are. Named by
    # `PlanFieldDescriptor.owner_title` rather than a path segment, so any fan-out axis reads
    # correctly. Never set by the compiler — only by `call_plan.focus_questions`, which is why
    # a compiled prompt renders byte-identically.
    still_needed: list[str] = Field(default_factory=list)
```

In `prompting.py`, in `_numbered_question`, immediately after the `fanned_codes` block (which ends at line 459) and before `if confirms:`:

```python
    if question.still_needed:
        lines.append(f"   - Still needed for: {', '.join(question.still_needed)}.")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_prompting.py -v
```

Expected: PASS, and `TestPanelsMatchThePrompt` still passes — the compiler sets no `still_needed`, so no compiled line moves.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/question_plan.py packages/vera_core/src/vera_core/forms/prompting.py tests/unit/forms/test_prompting.py
git commit -m "feat(forms): PromptQuestion.still_needed names a partial fan-out's owed members"
```

---

### Task 3: `render_digest` — the compact renderer

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py` (add after `numbered_questions`, line 377)
- Test: `tests/unit/forms/test_prompting.py`

**Interfaces:**
- Consumes: `PromptQuestion.still_needed` (Task 2).
- Produces: `render_digest(panels: list[PromptPanel]) -> str`

**Context you need:** this is for a reader that **already has** the full list in its system prompt and only needs pointing at entries — the two refusal messages. It must number identically to `render_panels`/`numbered_questions`: one continuous counter across every panel, and **no ordinal** for a routing question or a confirm node. Getting that wrong makes the refusal's ordinals disagree with the list the agent is reading.

The sole root section panel is omitted from the crumb because it names the task; repeating it on every line says nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/forms/test_prompting.py`:

```python
def _digest_tree() -> list[PromptPanel]:
    """One section panel over two service panels — the real compiled shape."""
    return [
        PromptPanel(
            title="Infertility Treatment",
            items=[
                PromptPanel(
                    title="Ovulation Induction (OI/TI)",
                    codes=Codes(icd10=["Z31.89"]),
                    items=[
                        PromptQuestion(
                            text="What is the cycle limit for ovulation induction?",
                            options=[PromptOption(target_paths=["a.oi.cycle"])],
                            gate_text="this service is covered",
                        )
                    ],
                ),
                PromptPanel(
                    title="IUI",
                    codes=Codes(cpt=["58323", "58322"]),
                    items=[
                        PromptQuestion(
                            text="What is the copay or coinsurance for IUI?",
                            options=[
                                PromptOption(label="Copay ($)", target_paths=["a.iui.copay"]),
                                PromptOption(label="Coinsurance (%)", target_paths=["a.iui.coins"]),
                            ],
                            gate_text="this service is covered",
                        ),
                        PromptQuestion(
                            text="What is the cycle limit for IUI?",
                            options=[PromptOption(target_paths=["a.iui.cycle"])],
                        ),
                    ],
                ),
            ],
        )
    ]


class TestRenderDigest:
    def test_a_crumb_is_printed_once_per_panel_with_its_codes(self) -> None:
        digest = render_digest(_digest_tree())
        assert "Ovulation Induction (OI/TI) [ICD ten Z31.89]:" in digest
        assert "IUI [CPT 58323, 58322]:" in digest
        # The sole root section panel names the task, so it never enters a crumb.
        assert "Infertility Treatment" not in digest
        assert digest.count("IUI [CPT 58323, 58322]:") == 1

    def test_numbering_is_continuous_across_panels(self) -> None:
        digest = render_digest(_digest_tree())
        assert "1. What is the cycle limit for ovulation induction?" in digest
        assert "2. What is the copay or coinsurance for IUI?" in digest
        assert "3. What is the cycle limit for IUI?" in digest

    def test_the_last_ordinal_is_numbered_questions(self) -> None:
        # The refusal's ordinals have to mean the same thing as the list the agent is reading.
        tree = _digest_tree()
        assert f"{numbered_questions(tree)}. What is the cycle limit for IUI?" in render_digest(tree)

    def test_either_or_labels_and_gate_are_carried_inline(self) -> None:
        digest = render_digest(_digest_tree())
        assert (
            "What is the copay or coinsurance for IUI? "
            "[either: Copay ($) / Coinsurance (%)] (only if this service is covered)"
        ) in digest

    def test_still_needed_is_carried_inline(self) -> None:
        panels = [
            PromptPanel(
                title="Labs",
                items=[
                    PromptQuestion(
                        text="Are codes 58340, 82670 covered?",
                        options=[
                            PromptOption(target_paths=["a.cpt_58340.cov", "a.cpt_82670.cov"])
                        ],
                        still_needed=["CPT 58340"],
                    )
                ],
            )
        ]
        assert "(still needed for: CPT 58340)" in render_digest(panels)

    def test_a_routing_question_takes_no_ordinal(self) -> None:
        panels = [
            PromptPanel(
                title="Egg cryo",
                items=[
                    PromptQuestion(text="Elective or cancer?", routes_between=["Elec", "Canc"]),
                    PromptQuestion(
                        text="Elective covered?",
                        options=[PromptOption(target_paths=["a.elec"])],
                    ),
                ],
            )
        ]
        digest = render_digest(panels)
        assert "First settle which applies: Elective or cancer?" in digest
        assert "Elec or Canc — only one applies" in digest
        assert "1. Elective covered?" in digest
        assert "1. Elective or cancer?" not in digest

    def test_a_confirm_node_takes_no_ordinal(self) -> None:
        panels = [
            PromptPanel(
                title="Basics",
                items=[
                    PromptQuestion(
                        text="Spouse name?", options=[PromptOption(target_paths=["a.spouse"])]
                    ),
                    PromptQuestion(
                        text="Read back the DOB",
                        options=[PromptOption(target_paths=["a.dob"])],
                        is_confirm=True,
                    ),
                ],
            )
        ]
        digest = render_digest(panels)
        assert "1. Spouse name?" in digest
        assert "Read back the DOB" in digest
        assert "2." not in digest

    def test_an_untitled_panel_yields_lines_with_no_crumb(self) -> None:
        # Hand-built fixtures (and any panel the compiler leaves untitled) still render.
        panels = [
            PromptPanel(
                items=[
                    PromptQuestion(text="Rep name?", options=[PromptOption(target_paths=["a.rep"])])
                ]
            )
        ]
        assert render_digest(panels) == "1. Rep name?"

    def test_an_empty_tree_renders_empty(self) -> None:
        assert render_digest([]) == ""
```

Add `render_digest` and `Codes` to that file's imports (it already imports `numbered_questions`, `render_panels`, `PromptOption`, `PromptPanel`, `PromptQuestion`; `Codes` comes from `vera_core.forms.dsl`).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_prompting.py::TestRenderDigest -v
```

Expected: collection error — `ImportError: cannot import name 'render_digest'`.

- [ ] **Step 3: Implement `render_digest`**

In `prompting.py`, add `from itertools import count, groupby` (the module already imports `count` from `itertools` at line 20 — extend that line rather than adding a second import), then add after `numbered_questions`:

```python
def render_digest(panels: list[PromptPanel]) -> str:
    """The same tree as `render_panels`, compressed for a reader that ALREADY has the full list.

    Each panel's crumb — its title chain plus the nearest codes line — is printed once, with its
    questions numbered beneath. Numbering is `render_panels`' own: one continuous counter across
    every panel, and no ordinal for a routing question or a confirm node, so a refusal's
    ordinals mean the same thing as the list the agent is reading. The sole root section panel
    is left out of the crumb: it names the task, so repeating it on every line says nothing.
    """
    numbering = count(1)
    entries: list[tuple[str, str]] = []
    bare_root = len(panels) == 1

    def walk(panel: PromptPanel, titles: list[str], codes: str, root: bool) -> None:
        here = titles if (root and bare_root) else [*titles, panel.title or ""]
        here = [title for title in here if title]
        codes = (_codes_text(panel.codes) if panel.codes is not None else "") or codes
        crumb = " > ".join(here)
        if codes:
            crumb = f"{crumb} [{codes}]" if crumb else f"[{codes}]"
        for item in panel.items:
            if isinstance(item, PromptPanel):
                walk(item, here, codes, False)
            elif item.routes_between:
                entries.append(
                    (
                        crumb,
                        f"* First settle which applies: {item.text} "
                        f"({' or '.join(item.routes_between)} — only one applies)",
                    )
                )
            elif item.is_confirm:
                entries.append((crumb, f"* {item.text}"))
            else:
                entries.append((crumb, f"{next(numbering)}. {_digest_line(item)}"))

    for panel in panels:
        walk(panel, [], "", True)

    blocks: list[str] = []
    for crumb, group in groupby(entries, key=lambda entry: entry[0]):
        lines = [line for _crumb, line in group]
        blocks.append("\n".join([f"{crumb}:", *(f"  {line}" for line in lines)]) if crumb else "\n".join(lines))
    return "\n\n".join(blocks)


def _digest_line(question: PromptQuestion) -> str:
    parts = [question.text]
    labels = [option.label for option in question.options if option.label]
    if labels:
        parts.append(f"[either: {' / '.join(labels)}]")
    if question.still_needed:
        parts.append(f"(still needed for: {', '.join(question.still_needed)})")
    if question.gate_text is not None:
        parts.append(f"(only if {question.gate_text})")
    return " ".join(parts)
```

`groupby` groups **consecutive** entries, which is what is wanted: a panel's questions are contiguous, and a routing question sitting in the parent panel between two child panels correctly starts its own block.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_prompting.py -v
```

Expected: PASS. If `ruff format` rewraps the long `blocks.append(...)` line, accept its formatting.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompting.py tests/unit/forms/test_prompting.py
git commit -m "feat(forms): render_digest renders an owed-question tree compactly"
```

---

### Task 4: `owner_title` — compile each leaf's nearest titled ancestor

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (add a field to `PlanFieldDescriptor` lines 87-104; add `_owner_titles`; set it in `compile_call_plan` lines 239-281)
- Test: `tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-3.
- Produces: `PlanFieldDescriptor.owner_title: str | None`

**Context you need:** this is what lets `still_needed` name a fan-out member without parsing a key. `authoring.cpt_group` titles itself `f"CPT {code}"`, so a CPT axis names itself; a future fan-out over some other axis names itself too. `Group.title` and `Section.title` are both required `str` in `dsl.py`, so a titled ancestor almost always exists — but the field stays `str | None` because a leaf directly under a section has no ancestor **group**, and Task 5 must be able to tell.

Use `doc._iter_fields()` (yields `(root-anchored path, field)` for groups **and** leaves, document order) and `doc.leaf_items()`. Sections are not yielded by `_iter_fields` — it starts at `section.fields` — so a section title can never be picked up as an owner, which is correct: the section is the panel, not the fan-out member.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/forms/test_call_plan.py`:

```python
def _descriptor(plan: CallPlan, suffix: str) -> PlanFieldDescriptor:
    return next(f for t in plan.tasks for f in t.fields if f.path.endswith(suffix))


class TestOwnerTitle:
    """`still_needed` names a fan-out's members by their owning group's title, never by a
    path segment, so it reads correctly on any schema."""

    def test_a_cpt_leaf_is_owned_by_its_cpt_group(self) -> None:
        field = _descriptor(PLAN, "labs_xray_ultrasound.cpt_58340.covered")
        assert field.owner_title == "CPT 58340"

    def test_a_service_level_leaf_is_owned_by_its_service_group(self) -> None:
        field = _descriptor(PLAN, "ovulation_induction.cycle_limit")
        assert field.owner_title == "Ovulation Induction/Timed Intercourse (OI/TI)"

    def test_a_leaf_directly_under_a_section_has_no_owning_group(self) -> None:
        # Only GROUPS own; a section is the panel a question sits under, not a fan-out member.
        field = _descriptor(PLAN, "infertility_treatment.infertility_tx_covered")
        assert field.owner_title is None

    def test_every_descriptor_of_both_catalogs_compiles(self) -> None:
        # disease_only has no ask groups at all; owner_title must still be well-defined.
        for doc in (build_ibv_standard(), build_disease_only()):
            plan = compile_call_plan(
                doc, None, schema_version_id=uuid4(), prompt_version_id=None
            )
            for task in plan.tasks:
                for field in task.fields:
                    assert field.owner_title is None or field.owner_title
```

Add `from vera_core.forms.catalog.disease_only import build_disease_only` to that file's imports. `PLAN`, `CallPlan`, `PlanFieldDescriptor`, `compile_call_plan`, `build_ibv_standard` and `uuid4` are all already imported there.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_call_plan.py::TestOwnerTitle -v
```

Expected: FAIL — `AttributeError: 'PlanFieldDescriptor' object has no attribute 'owner_title'`.

- [ ] **Step 3: Compile `owner_title`**

In `call_plan.py`, add to `PlanFieldDescriptor` after `exclusive_note` (line 104):

```python
    # Nearest titled ancestor GROUP — how `still_needed` names this leaf when a fan-out owes
    # only some of its members. None for a leaf sitting directly under its section.
    owner_title: str | None = None
```

Add this helper next to `_exclusive_notes` (line 205):

```python
def _owner_titles(doc: FormSchemaDoc) -> dict[str, str]:
    """`{leaf path: nearest titled ancestor group's title}`.

    Sections are deliberately out of reach — `_iter_fields` starts at `section.fields` — because
    the section is the panel a question sits under, not a member of the fan-out."""
    groups = {
        path: field.title
        for path, field in doc._iter_fields()
        if isinstance(field, Group) and field.title
    }
    owners: dict[str, str] = {}
    for path, _leaf in doc.leaf_items():
        parts = path.split(".")
        for cut in range(len(parts) - 1, 1, -1):
            title = groups.get(".".join(parts[:cut]))
            if title is not None:
                owners[path] = title
                break
    return owners
```

In `compile_call_plan`, next to `exclusive_notes = _exclusive_notes(doc)` (line 256):

```python
    owner_titles = _owner_titles(doc)
```

and in the `PlanFieldDescriptor(...)` construction, after `exclusive_note=exclusive_notes.get(path),`:

```python
                owner_title=owner_titles.get(path),
```

`Group` is already imported in `call_plan.py` (line 55) — check before re-adding.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

`owner_title` lives on `PlanFieldDescriptor` (the runtime CallPlan), not on the schema document, so `data/form_schemas/*.json` is untouched and `test_schema_dsl.py`'s freshness check is unaffected. If it does fail, something else changed — investigate rather than regenerating.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py tests/unit/forms/test_call_plan.py
git commit -m "feat(forms): compile owner_title onto each plan field descriptor"
```

---

### Task 5: `focus_questions` — the tree × descriptor join

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (add after `owed_now`, line 187)
- Test: `tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: `keep_questions` (Task 1), `PromptQuestion.still_needed` (Task 2), `PlanFieldDescriptor.owner_title` (Task 4).
- Produces:
  ```python
  def focus_questions(
      task: PlanTask,
      paths: Collection[str],
      answers: Mapping[str, Any],
      shared: dict[str, Condition],
      *,
      explode: bool = False,
  ) -> list[PromptPanel]
  ```

**Context you need:** two jobs. It stamps `still_needed` when a kept question's owed targets are a **strict subset** of its targets, and — when `explode=True` — it grows the path set to the transitive closure over gates, so a question whose gate reads a path being re-asked comes along carrying its own `Ask only if …` prose. That pre-loading is what closes the gap-pass blind spot: the Observer extracts in a detached pass, so on the turn right after the rep confirms coverage the follow-ups are not yet owed, and the agent has an answer with no sanctioned next question.

Named for the general `(task, paths) -> tree` operation, not for the owed set, because the focused-retry branch will call it with a focus set.

`shared` must be typed `dict[str, Condition]` (not `Mapping`) because it is forwarded to `condition_field_paths`, which declares `dict[str, Condition] | None`. mypy `--strict` rejects `Mapping` there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/forms/test_call_plan.py`:

```python
def _focus_task() -> PlanTask:
    """One service: a 2-code fanned `covered`, plus a copay gated on it."""
    covered = ("s.cpt_1.covered", "s.cpt_2.covered")
    gate = AnyCondition(any=[eq(covered[0], "Yes"), eq(covered[1], "Yes")])
    return PlanTask(
        task_key="t",
        title="T",
        prompt="p",
        fields=[
            PlanFieldDescriptor(
                path=covered[0], title="Covered", type="enum", role="ask",
                required=True, owner_title="CPT 1",
            ),
            PlanFieldDescriptor(
                path=covered[1], title="Covered", type="enum", role="ask",
                required=True, owner_title="CPT 2",
            ),
            PlanFieldDescriptor(
                path="s.copay", title="Copay ($)", type="currency", role="ask",
                required=True, gates=(gate,), owner_title="Service",
            ),
        ],
        panels=[
            PromptPanel(
                title="Service",
                items=[
                    PromptQuestion(
                        text="Are codes 1, 2 covered?",
                        options=[PromptOption(target_paths=list(covered))],
                    ),
                    PromptQuestion(
                        text="What is the copay?",
                        options=[PromptOption(target_paths=["s.copay"])],
                        gate_text="this service is covered",
                    ),
                ],
            )
        ],
    )


class TestFocusQuestions:
    def test_it_keeps_only_the_questions_that_answer_the_paths(self) -> None:
        panels = focus_questions(_focus_task(), ["s.copay"], {}, {})
        assert [q.text for q in iter_questions(panels)] == ["What is the copay?"]

    def test_a_fully_owed_fan_out_is_not_stamped(self) -> None:
        panels = focus_questions(
            _focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {}
        )
        question = next(iter_questions(panels))
        assert question.still_needed == []

    def test_a_partly_owed_fan_out_names_the_members_it_still_needs(self) -> None:
        panels = focus_questions(_focus_task(), ["s.cpt_2.covered"], {}, {})
        question = next(iter_questions(panels))
        assert question.still_needed == ["CPT 2"]

    def test_explode_pulls_in_a_question_gated_on_an_owed_path(self) -> None:
        panels = focus_questions(
            _focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {}, explode=True
        )
        assert [q.text for q in iter_questions(panels)] == [
            "Are codes 1, 2 covered?",
            "What is the copay?",
        ]
        # The dependent keeps its own condition, which is what makes it a FOLLOW-UP and not
        # something to ask unconditionally.
        assert next(q for q in iter_questions(panels) if q.text == "What is the copay?").gate_text

    def test_explode_leaves_an_already_answered_dependent_alone(self) -> None:
        panels = focus_questions(
            _focus_task(),
            ["s.cpt_1.covered", "s.cpt_2.covered"],
            {"s.copay": "$30"},
            {},
            explode=True,
        )
        assert [q.text for q in iter_questions(panels)] == ["Are codes 1, 2 covered?"]

    def test_without_explode_the_dependent_stays_out(self) -> None:
        panels = focus_questions(
            _focus_task(), ["s.cpt_1.covered", "s.cpt_2.covered"], {}, {}
        )
        assert [q.text for q in iter_questions(panels)] == ["Are codes 1, 2 covered?"]

    def test_a_member_with_no_owner_title_suppresses_the_clause(self) -> None:
        # Better to say nothing than to name a member the agent cannot act on.
        task = _focus_task()
        task.fields[1].owner_title = None
        panels = focus_questions(task, ["s.cpt_2.covered"], {}, {})
        assert next(iter_questions(panels)).still_needed == []

    def test_explode_reaches_a_fixpoint_on_the_real_schema(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        owed = ["sections.infertility_treatment.embryo_biopsy.cpt_89290.covered"]
        exploded = focus_questions(task, owed, {}, PLAN.shared_conditions, explode=True)
        texts = [q.text for q in iter_questions(exploded)]
        assert any("covered under this plan" in t for t in texts)
        assert any("copay or coinsurance" in t for t in texts)
        assert any("cycle limit" in t for t in texts)
        # Idempotent: exploding the exploded set adds nothing.
        again = focus_questions(
            task,
            [p for q in iter_questions(exploded) for p in q.target_paths],
            {},
            PLAN.shared_conditions,
            explode=True,
        )
        assert [q.text for q in iter_questions(again)] == texts
```

Add `focus_questions` to the `vera_core.forms.call_plan` import block and `AnyCondition` to the `vera_core.forms.dsl` one, plus `from vera_core.forms.authoring import eq`. `PLAN`, `plan_task`, `PlanTask`, `PlanFieldDescriptor`, `iter_questions`, `PromptOption`, `PromptPanel` and `PromptQuestion` are already imported there.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_call_plan.py::TestFocusQuestions -v
```

Expected: collection error — `ImportError: cannot import name 'focus_questions'`.

- [ ] **Step 3: Implement `focus_questions`**

In `call_plan.py`, add after `owed_now` (line 187):

```python
def focus_questions(
    task: PlanTask,
    paths: Collection[str],
    answers: Mapping[str, Any],
    shared: dict[str, Condition],
    *,
    explode: bool = False,
) -> list[PromptPanel]:
    """`task`'s question tree narrowed to `paths`, each partly-owed fan-out told which members
    it still needs.

    `explode` grows the set to the transitive closure over gates: a question whose gate reads a
    path being asked comes along, carrying its own `Ask only if …` prose. That pre-loads the
    follow-ups an answer is about to open — the Observer extracts in a detached pass, so on the
    turn right after the representative confirms coverage they are not yet owed, and an agent
    holding an answer with no sanctioned next question is an agent inventing one.

    Named for the operation, not for the owed set: the focused-retry path narrows the same tree
    against a different path set.
    """
    by_path = {field.path: field for field in task.fields}
    wanted = _exploded(task, set(paths), answers, shared, by_path) if explode else set(paths)
    return _stamp_still_needed(keep_questions(task.panels, wanted), wanted, by_path)


def _exploded(
    task: PlanTask,
    wanted: set[str],
    answers: Mapping[str, Any],
    shared: dict[str, Condition],
    by_path: Mapping[str, PlanFieldDescriptor],
) -> set[str]:
    """`wanted` plus every question gated on something already in it, to a fixpoint."""
    questions = [q for q in iter_questions(task.panels) if q.target_paths]
    wanted = set(wanted)
    while True:
        grew = False
        for question in questions:
            if not wanted.isdisjoint(question.target_paths):
                continue
            if all(has_value(answers, path) for path in question.target_paths):
                continue  # on file already; a follow-up nobody owes is noise
            refs = {
                ref
                for path in question.target_paths
                if (field := by_path.get(path)) is not None
                for gate in field.gates
                for ref in condition_field_paths(gate, shared)
            }
            if not wanted.isdisjoint(refs):
                wanted.update(question.target_paths)
                grew = True
        if not grew:
            return wanted


def _stamp_still_needed(
    panels: list[PromptPanel],
    wanted: set[str],
    by_path: Mapping[str, PlanFieldDescriptor],
) -> list[PromptPanel]:
    """`still_needed` on every question owing only SOME of its targets.

    Suppressed unless every owed target has an `owner_title`: a half-named list is worse than
    none, because the agent would read it as the complete remainder."""

    def question(node: PromptQuestion) -> PromptQuestion:
        owed = [path for path in node.target_paths if path in wanted]
        if not owed or len(owed) == len(node.target_paths):
            return node
        titles = [
            title
            for path in owed
            if (field := by_path.get(path)) is not None and (title := field.owner_title)
        ]
        if len(titles) != len(owed):
            return node
        return node.model_copy(update={"still_needed": list(dict.fromkeys(titles))})

    def panel(node: PromptPanel) -> PromptPanel:
        return node.model_copy(
            update={
                "items": [
                    panel(item) if isinstance(item, PromptPanel) else question(item)
                    for item in node.items
                ]
            }
        )

    return [panel(node) for node in panels]
```

Add `keep_questions` and `condition_field_paths` to the existing import blocks in `call_plan.py` (it already imports `PromptPanel`, `PromptQuestion`, `iter_questions`, `hydrate_panels` from `question_plan`, and `has_value` from `conditions`). `Collection` and `Mapping` are already imported from `collections.abc` (line 34).

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/unit/forms/test_call_plan.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py tests/unit/forms/test_call_plan.py
git commit -m "feat(forms): focus_questions narrows a task's tree with optional gate closure"
```

---

### Task 6: Wire the `task_complete` refusal

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` (`PlanTaskAgent.__init__` ~line 242, `_apply_gating` lines 327-349, `_refuse_premature_completion` lines 397-438; `PlanRunController` — add `answers` property and `gap_panels`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: `focus_questions` (Task 5), `render_digest` (Task 3).
- Produces:
  - `PlanRunController.answers -> Mapping[str, Any]` (read-only)
  - `PlanRunController.gap_panels(task_index: int, fields: list[PlanFieldDescriptor], *, explode: bool = False) -> list[PromptPanel]`
  - `PlanTaskAgent._panels: list[PromptPanel]` — the tree currently in this agent's instructions

**Context you need:** `_apply_gating` narrows the task's tree at entry and **throws the result away**. A refusal narrowing from `self._task.panels` could therefore name a question the agent's own instructions do not contain, so the narrowed tree has to be stored.

`gap_panels` pre-prunes with `drop_questions(panels, excluded)` before narrowing. `gap_fields` already filters by `is_applicable`, but the Task 5 explode closure could otherwise surface a question some *other* gate has decidably ruled out. It lives on the controller because `excluded_fields` needs controller state (`_answers`, `_decided_false`) that `call_plan` cannot see.

**Do not change any guard arithmetic.** `_questions_at_entry`, the refusal budget and the turn ceiling keep their current behavior — this task changes only the message text.

- [ ] **Step 1: Write the failing test**

Append to `apps/agent_worker/tests/unit/test_plan_runtime.py`:

```python
def _titled_gap_plan() -> CallPlan:
    """Two services whose leaf titles COLLIDE — the real CPT shape. Only the panel titles tell
    the two "Covered" questions apart, which is the whole point of the refusal rewrite."""
    fields = [
        _field("sections.t.elective.cpt_89337.covered", "Covered", values=["Yes", "No"]),
        _field("sections.t.cancer.cpt_89337.covered", "Covered", values=["Yes", "No"]),
        _field("sections.t.elective.cycle_limit", "Cycle Limit"),
    ]
    for field in fields:
        field.owner_title = "CPT 89337" if "cpt_89337" in field.path else "Egg Cryo Elective"
    closing = [_field("sections.close.ref_number", "Reference number")]
    panels = [
        PromptPanel(
            title="Infertility Treatment",
            items=[
                PromptPanel(
                    title="Egg Cryopreservation Elective",
                    codes=Codes(cpt=["89337"]),
                    items=[
                        _question("Is 89337 for elective egg cryo covered?", fields[0].path),
                        _question("What is the cycle limit for elective egg cryo?", fields[2].path),
                    ],
                ),
                PromptPanel(
                    title="Egg Cryopreservation Cancer",
                    codes=Codes(cpt=["89337"]),
                    items=[_question("Is 89337 for cancer-related egg cryo covered?", fields[1].path)],
                ),
            ],
        )
    ]
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="treatment", title="Treatment", intro="Hello rep.",
                prompt="Treatment.", fields=fields, panels=panels,
            ),
            PlanTask(
                task_key="closing_task", title="Wrap Up", prompt="Close.",
                fields=closing, panels=_panels_for(closing),
            ),
        ],
    )


class TestRefusalNamesTheService:
    """The defect: two "Cycle Limit" lines and two "Covered (cpt_89337)" lines, with nothing
    saying which service either belonged to."""

    @pytest.mark.asyncio
    async def test_the_refusal_names_each_owed_question_under_its_service(self) -> None:
        controller, _ = _controller(_titled_gap_plan())
        agent = await _enter(controller, 0)
        refusal = await _tool(agent, "task_complete")()
        assert isinstance(refusal, str)
        assert "Egg Cryopreservation Elective [CPT 89337]:" in refusal
        assert "Egg Cryopreservation Cancer [CPT 89337]:" in refusal
        assert "Is 89337 for elective egg cryo covered?" in refusal
        assert "Is 89337 for cancer-related egg cryo covered?" in refusal
        # The old rendering: a bare storage-field title with no subject.
        assert "- Covered (cpt_89337)" not in refusal
        # The section panel names the task, so it never repeats on a line.
        assert "Infertility Treatment" not in refusal

    @pytest.mark.asyncio
    async def test_the_refusal_lists_only_what_is_still_owed(self) -> None:
        controller, _ = _controller(_titled_gap_plan())
        controller.update_answers(
            {"sections.t.cancer.cpt_89337.covered": "No"}
        )
        agent = await _enter(controller, 0)
        refusal = await _tool(agent, "task_complete")()
        assert isinstance(refusal, str)
        assert "Egg Cryopreservation Cancer" not in refusal
        assert "Egg Cryopreservation Elective [CPT 89337]:" in refusal
```

Add `Codes` (from `vera_core.forms.dsl`) to that file's imports if absent; `PromptPanel`, `_question`, `_field`, `_panels_for`, `_controller`, `_enter`, `_tool` already exist in it.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestRefusalNamesTheService -v
```

Expected: FAIL — the refusal still renders `- Covered (cpt_89337)` and contains no crumb line.

- [ ] **Step 3: Implement**

In `plan_runtime.py`:

Extend the imports:
```python
from vera_core.forms.call_plan import (
    CallPlan,
    PlanFieldDescriptor,
    focus_questions,
    gating_seed,
    owed_now,
)
from vera_core.forms.prompting import numbered_questions, render_digest, render_panels
```

In `PlanTaskAgent.__init__`, next to `self._questions_at_entry = 0`:
```python
        # The tree currently in this agent's instructions. A refusal must narrow from THIS and
        # not from the compiled tree, or it can name a question the agent cannot see.
        self._panels = self._task.panels
```

At the end of `_apply_gating`, replace `return kept` with:
```python
        self._panels = kept
        return kept
```
and in its early-return branch (`if not excluded or not self._task.panels:`) leave `return self._task.panels` as is — `self._panels` already holds it.

Replace the return statement of `_refuse_premature_completion` (lines 433-438) with:
```python
        return (
            "Not yet — these required questions of the current task have no answer on file. "
            "Ask the representative for them now (one at a time), and call task_complete once "
            "they are answered or the representative says they cannot answer:\n"
            f"{render_digest(self._owed_digest(outstanding))}"
        )
```

and add to `PlanTaskAgent`:
```python
    def _owed_digest(self, outstanding: list[PlanFieldDescriptor]) -> list[PromptPanel]:
        """`outstanding` as a narrowing of the tree this agent's instructions already show."""
        return focus_questions(
            self._task.model_copy(update={"panels": self._panels}),
            [field.path for field in outstanding],
            self._controller.answers,
            self._controller.plan.shared_conditions,
        )
```

In `PlanRunController`, add next to `update_answers`:
```python
    @property
    def answers(self) -> Mapping[str, Any]:
        """Baseline plus what the call has collected. Read-only: `update_answers` is the writer."""
        return self._answers
```

and in the gap-pass API section, next to `gap_fields`:
```python
    def gap_panels(
        self,
        task_index: int,
        fields: list[PlanFieldDescriptor],
        *,
        explode: bool = False,
    ) -> list[PromptPanel]:
        """This task's tree narrowed to `fields`, gated-out questions pruned first.

        The pre-prune matters only for `explode`: `gap_fields` is already applicable-only, but
        the closure could otherwise surface a question some OTHER gate has decidably ruled out.
        It lives here rather than in `call_plan` because `excluded_fields` needs this
        controller's answers."""
        task = self.plan.tasks[task_index]
        excluded = {field.path for field in self.excluded_fields(task_index)}
        panels = drop_questions(task.panels, excluded) if excluded else task.panels
        return focus_questions(
            task.model_copy(update={"panels": panels}),
            [field.path for field in fields],
            self._answers,
            self.plan.shared_conditions,
            explode=explode,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: `TestRefusalNamesTheService` passes, and every pre-existing test in the file still passes. `TestFieldLines` is untouched here (Task 7 removes it), and no existing test asserts on the refusal's text — verified with `rg -n 'Not yet' apps/agent_worker/tests/`, which matches only fixture *inputs* in `tests/evals/test_transcript.py`, never an assertion on this output. So a failure elsewhere in this file is a real regression, not a stale assertion.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(agent): task_complete refusal names each owed question's service and codes"
```

---

### Task 7: Wire the gap agent, and delete the field-line renderers

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` (`_gap_block` lines 96-121; delete `_owning_segment`/`_field_line`/`_field_lines` lines 156-187; `GapTaskAgent.__init__`, `_build_instructions`, `_apply_gap_list`, `_refuse_premature_gap_complete`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: `PlanRunController.gap_panels` (Task 6), `render_panels`/`render_digest`/`numbered_questions`.
- Produces: `_gap_block(title: str, required: int, panels: list[PromptPanel]) -> str`

**Context you need:** the gap agent's system instruction is the one place with **no** other question list, so it gets the full `render_panels` output of the **exploded** tree. Its refusal gets the digest of the **unexploded** tree, because by then the agent does have a list.

The lead-in count `required` is `numbered_questions` of the **unexploded** narrowing — the count `_refuse_premature_gap_complete`'s ceiling actually enforces. Never claim the exploded total as owed.

The old prose (*"keep going until every item on it has been asked — the list is the complete set"*, *"do not shorten the LIST"*) **must go**: with follow-ups pre-loaded it would push the agent to ask conditional questions unconditionally.

`GapTaskAgent.__init__` builds instructions before any answer snapshot exists, so the empty branch stays.

- [ ] **Step 1: Write the failing tests**

Append to `apps/agent_worker/tests/unit/test_plan_runtime.py`:

```python
class TestGapInstructionCarriesContext:
    """The gap agent has no other question list, so its instructions are the whole context."""

    @pytest.mark.asyncio
    async def test_the_gap_list_names_services_and_pre_loads_gated_follow_ups(self) -> None:
        controller, _ = _controller(_titled_gap_plan())
        # Enter the task, then walk to the gap pass over it.
        await _enter(controller, 0)
        gap = controller.gap_agents[0]
        with _session_patch(gap, MagicMock()):
            await gap.on_enter()
        assert "Egg Cryopreservation Elective" in gap.instructions
        assert "Is 89337 for elective egg cryo covered?" in gap.instructions
        # The tier marker, and the retired absolutist wording.
        assert 'A question marked "Ask only if ..." is a follow-up' in gap.instructions
        assert "the list is the complete set" not in gap.instructions
        assert "do not shorten the LIST" not in gap.instructions

    @pytest.mark.asyncio
    async def test_the_lead_in_counts_the_required_questions_not_the_follow_ups(self) -> None:
        controller, _ = _controller(_titled_gap_plan())
        await _enter(controller, 0)
        gap = controller.gap_agents[0]
        required = numbered_questions(
            controller.gap_panels(0, controller.gap_fields(0))
        )
        with _session_patch(gap, MagicMock()):
            await gap.on_enter()
        assert f"{required} required question" in gap.instructions

    @pytest.mark.asyncio
    async def test_the_gap_refusal_uses_the_digest(self) -> None:
        controller, _ = _controller(_titled_gap_plan())
        await _enter(controller, 0)
        gap = controller.gap_agents[0]
        with _session_patch(gap, MagicMock()):
            await gap.on_enter()
            refusal = await _tool(gap, "gap_complete")()
        assert isinstance(refusal, str)
        assert "Egg Cryopreservation Elective [CPT 89337]:" in refusal
        assert "- Covered (cpt_89337)" not in refusal
```

Add `numbered_questions` to the file's imports from `vera_core.forms.prompting`.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestGapInstructionCarriesContext -v
```

Expected: FAIL — the instructions still contain `the list is the complete set` and no crumb.

- [ ] **Step 3: Implement**

Replace `_gap_block` (lines 96-121) with:

```python
def _gap_block(title: str, required: int, panels: list[PromptPanel]) -> str:
    """Instruction block for a gap agent: what it still owes, and the follow-ups those answers
    will open.

    The follow-ups are pre-loaded because the Observer extracts in a detached pass — on the turn
    right after the representative confirms coverage they are not yet owed, and an agent holding
    an answer with no sanctioned next question invents one. They carry their own condition, so
    one list expresses both tiers; `required` counts only the first."""
    if not panels:
        owed = (
            "Required questions from earlier in the call are still unanswered. When the list "
            "arrives, re-ask ONLY those specific questions, politely, one at a time."
        )
    else:
        subject = "question is" if required == 1 else "questions are"
        owed = (
            f"{required} required {subject} still unanswered from earlier in the call. Ask every "
            "question below whose condition holds, politely, one at a time, and re-ask ONLY "
            'questions from this list. A question marked "Ask only if ..." is a follow-up: ask '
            "it only once its condition is true — typically right after the representative "
            f"confirms coverage.\n{render_panels(panels)}"
        )
    return (
        f"# Current task: Follow-up questions ({title})\n"
        f"{owed}\n"
        "Keep each question brief. If the representative cannot answer one, accept it and move "
        "to the next one; never press or repeat. This is a mid-call follow-up, NOT the end of "
        "the call: do NOT say goodbye, do NOT thank the representative as if finishing, and do "
        "NOT claim you have everything you need — more questions may still follow. Once every "
        "question whose condition holds has been asked, call gap_complete."
    )
```

Delete `_owning_segment`, `_field_line` and `_field_lines` (lines 156-187) and drop the now-unused `Counter` import from `collections` (line 27).

In `GapTaskAgent.__init__`, replace `self._listed_paths: tuple[str, ...] = ()` with:
```python
        # The gap block currently in the instructions. Keyed on the TEXT, not the owed paths:
        # `still_needed` and the follow-up filter both read answers, so a path-keyed cache
        # would serve a stale list.
        self._listed_block = ""
```
and change the `super().__init__` call to `super().__init__(instructions=self._build_instructions(_gap_block(self._task.title, 0, [])))`.

Replace `_build_instructions` and `_apply_gap_list` with:

```python
    def _build_instructions(self, block: str) -> str:
        return _instructions(
            self._controller.plan,
            block,
            extra_instructions=self._controller.extra_instructions,
        )

    def _gap_text(self, fields: list[PlanFieldDescriptor]) -> str:
        """This sweep's block: the required count off the unexploded narrowing, the list off the
        exploded one."""
        if not fields:
            return _gap_block(self._task.title, 0, [])
        index = self._task_index
        required = numbered_questions(self._controller.gap_panels(index, fields))
        return _gap_block(
            self._task.title,
            required,
            self._controller.gap_panels(index, fields, explode=True),
        )

    async def _apply_gap_list(self, fields: list[PlanFieldDescriptor]) -> None:
        """Put this sweep's questions in the INSTRUCTIONS, where they outlive the turn that
        named them — the `_apply_gating` seam, and rebuilt not appended for the reason given
        there."""
        block = self._gap_text(fields)
        if block == self._listed_block:
            return
        self._listed_block = block
        await self.update_instructions(self._build_instructions(block))
```

Replace the return statement of `_refuse_premature_gap_complete` (lines 657-662) with:
```python
        return (
            f"Not yet — {len(outstanding)} of the follow-up questions you were given still have "
            "no answer on file. Ask the representative for them now, one at a time, and call "
            "gap_complete only once every one of them has been asked:\n"
            f"{render_digest(self._controller.gap_panels(self._task_index, outstanding))}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: the new class passes. `TestFieldLines` now fails to import `_field_lines` — **delete that class** (lines 1946-1980); Task 8 replaces its coverage. Update `_INTAKE_GAPS` (line 1333) to the question texts `_panels_for` produces, which are the field titles verbatim:
```python
# Every question `_multi_gap_plan()`'s intake task owes, as the digest renders it.
_INTAKE_GAPS = (
    "Representative name",
    "Call reference",
    "Covered",
    "Deductible",
)
```
The two `Covered` fields collapse to one entry because `_panels_for` gives each field its own question and the assertions are substring checks; a titled fixture is Task 8's job.

- [ ] **Step 5: Run the full gate**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(agent): gap instruction pre-loads gated follow-ups with service context"
```

---

### Task 8: Real-schema regression across both catalogs, then simplify

**Files:**
- Test: `tests/unit/forms/test_call_plan.py`
- Modify (from the simplify pass): whatever it touches

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: no new API.

**Context you need:** every test so far uses hand-built fixtures. This locks the behavior against the **real** compiled documents, including the exact ambiguity from the bug report: two `Cycle Limit` questions and two `89337` services. `disease_only` is the negative case — 44 questions, **0** fan-outs, **0** routing questions — so it proves the narrowing degrades to a plain subtree filter rather than assuming a fan-out exists.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/forms/test_call_plan.py`:

```python
class TestRealSchemaDigest:
    """The reported defect, against the real documents: seven ambiguous field titles."""

    def test_the_two_cycle_limits_and_two_89337_services_are_distinguishable(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        base = "sections.infertility_treatment"
        owed = [
            f"{base}.ovulation_induction.cycle_limit",
            f"{base}.intrauterine_insemination.cycle_limit",
            f"{base}.egg_cryopreservation_elective.cpt_89337.covered",
            f"{base}.egg_cryopreservation_cancer.cpt_89337.covered",
            f"{base}.frozen_embryo_transfer.cpt_58974.covered",
            f"{base}.embryo_biopsy.cpt_89290.covered",
            f"{base}.embryo_biopsy.cpt_89291.covered",
        ]
        panels = focus_questions(task, owed, {}, PLAN.shared_conditions)
        digest = render_digest(panels)
        assert "Ovulation Induction/Timed Intercourse (OI/TI)" in digest
        assert "Intrauterine Insemination (IUI) [CPT 58323, 58322, 89261" in digest
        assert "Egg Cryopreservation Elective [CPT 89337" in digest
        assert "Egg Cryopreservation Cancer [CPT 89337" in digest
        # Seven owed FIELDS, six spoken asks: 89290 and 89291 are one AskGroup question.
        assert numbered_questions(panels) == 6
        assert digest.count("cycle limit") == 2
        # The routing question survives because BOTH egg cryo branches are owed.
        assert "First settle which applies" in digest

    def test_one_egg_cryo_branch_owed_drops_the_routing_question(self) -> None:
        task = plan_task(PLAN, "infertility_coverage")
        owed = [
            "sections.infertility_treatment.egg_cryopreservation_elective.cpt_89337.covered"
        ]
        digest = render_digest(focus_questions(task, owed, {}, PLAN.shared_conditions))
        assert "Egg Cryopreservation Elective [CPT 89337" in digest
        assert "First settle which applies" not in digest

    def test_a_partly_owed_eight_code_fan_out_names_the_two_it_needs(self) -> None:
        task = plan_task(PLAN, "diagnostic_coverage")
        base = "sections.diagnostic_testing.labs_xray_ultrasound"
        owed = [f"{base}.cpt_58340.covered", f"{base}.cpt_82670.covered"]
        digest = render_digest(focus_questions(task, owed, {}, PLAN.shared_conditions))
        assert "(still needed for: CPT 58340, CPT 82670)" in digest

    def test_a_focused_plan_narrows_without_crashing(self) -> None:
        # focus_call_plan narrows descriptors but NOT panels (a known bug, fixed on a later
        # branch). Until then the new code paths must degrade rather than raise: every
        # descriptor lookup misses, so the closure does not explode and no clause is stamped.
        focused = focus_call_plan(
            PLAN,
            ["sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.covered"],
        )
        for task in focused.tasks:
            owed = [field.path for field in task.fields]
            panels = focus_questions(task, owed, {}, PLAN.shared_conditions, explode=True)
            render_digest(panels)
            assert all(not question.still_needed for question in iter_questions(panels))

    def test_both_catalogs_narrow_and_render_every_task(self) -> None:
        # disease_only has no ask groups and no routing questions; the narrowing must not
        # assume either exists.
        for doc in (build_ibv_standard(), build_disease_only()):
            plan = compile_call_plan(
                doc, None, schema_version_id=uuid4(), prompt_version_id=None
            )
            for task in plan.tasks:
                owed = [field.path for field in task.fields]
                panels = focus_questions(task, owed, {}, plan.shared_conditions)
                # Everything owed means everything kept — a mismatch is a real tree/descriptor
                # disagreement, so investigate it rather than relaxing this.
                assert numbered_questions(panels) == numbered_questions(task.panels), task.task_key
                render_digest(panels)
                focus_questions(task, owed, {}, plan.shared_conditions, explode=True)
```

Add `render_digest` and `numbered_questions` to that file's `vera_core.forms.prompting` import block.

- [ ] **Step 2: Run the tests to verify they pass or fail informatively**

```bash
uv run pytest tests/unit/forms/test_call_plan.py::TestRealSchemaDigest -v
```

These exercise already-implemented code, so they may pass immediately — that is the point of a regression lock. If one fails, the assertion encodes the spec's intent: fix the **implementation**, not the assertion, unless the assertion misstates the schema (verify with `rg` against `catalog/ibv_standard.py` before changing it).

- [ ] **Step 3: Run the simplify pass**

This is a mandatory repo rule (`CLAUDE.md`), not optional. In this same session, trigger it with exactly:

```
simplify code
```

Let it reconcile the code added in Tasks 1-7 — behavior must not change. Pay particular attention to `render_digest`'s `blocks.append` line and whether `_stamp_still_needed`'s tree recursion should reuse the `hydrate_panels` shape already in `question_plan.py`.

- [ ] **Step 4: Re-run the full gate on the simplified tree**

```bash
VERA_DATABASE_URL="postgresql+asyncpg://vera:vera@localhost:5432/task_complete_prompt_fix" just check
```

Expected: green. The last gate run must be on the exact tree being pushed.

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend
git commit -m "test(forms): lock the contextual owed-question digest against both catalogs"
```

- [ ] **Step 6: Clean up the test database**

```bash
docker compose exec -T postgres psql -U vera -d postgres -c 'DROP DATABASE "task_complete_prompt_fix_test";'
```

---

## Verification beyond the gate

`just check` asserts on **strings**; the defect being fixed lives in what the agent says. Per the backend `CLAUDE.md`, a green gate does **not** verify this change. Before claiming it done:

1. **Eval harness** — replays whole calls through the real entrypoint and compiled plan, and exercises the gap pass and both completion guards:
   ```bash
   VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
   ```
   `-m evals` is **required** — without it you get the LLM-free tests and no simulations, which looks like a clean pass. Confirm a real run by the `===== <scenario>: … =====` banners. Needs Vertex ADC and a seeded Postgres. Do not add these to `just check` (live LLM cost, run-to-run variance).
2. **A live call** via browser-callee transport — set `VERA_BROWSER_CALLEE_TRANSPORT=true` on both `just api` and `just worker` (plus `VITE_BROWSER_CALLEE_TRANSPORT=true` on the frontend) and join the room from Live Monitoring as the payer rep. Drive a task to a `task_complete` refusal and into the gap pass, and read the two messages in the trace.
3. **Do not overclaim from a green eval run.** No STT and no real DTMF, and extraction settles between turns, so rules fire more reliably than on a real call.

## Out of scope — do not touch

- `focus_call_plan`, `bookend_paths`, `queue_dispatcher.py`, `expand_to_groups`. The focused-retry fix is a separate branch; the spec's *Serving the retry fix* section records the seam and the evidence.
- `gap_fields`' field granularity.
- `render_panels`' output for a compiled prompt.
- The end-of-task confirm limitation documented on `owed_now`.
