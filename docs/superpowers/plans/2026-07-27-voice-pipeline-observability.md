# Voice Pipeline Observability Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make task/agent identity, every handoff (task-complete, rule-engine-forced, IVR→verification), and LLM-call purpose visible and queryable in Langfuse, plus correlate control-plane dispatch spans into the same trace as the call that follows.

**Architecture:** Hybrid instrumentation — tag the ambient LiveKit SDK span with `vera.*` attributes wherever it's already correctly scoped (task entry, task-complete handoff, IVR handoff), and open a Vera-owned `tracer.start_as_current_span(...)` wherever no correctly-scoped ambient span exists (rule-engine evaluation, the two background LLM call sites, three new control-plane dispatch spans). Every attribute is metadata only — schema-authored keys, closed enums, booleans, counts — never transcript/answer/DTMF content.

**Tech Stack:** Python 3.12, `opentelemetry-sdk` (already a dependency via `vera_core.observability.otel`), `livekit-agents`, pytest + `pytest-asyncio` (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-07-27-voice-pipeline-observability-design.md` — read it first; this plan implements it section by section (§5.1–§5.7).

## Global Constraints

- **PHI guardrail (design §6, hard requirement):** every new attribute is one of: a schema-authored structural identifier matching `^[a-z][a-z0-9_]*$` (`task_key`, `rule_key`) or a fixed `"@..."` sentinel, a closed enum, a boolean, or a count. **Never**: transcript text, `ExtractedAnswer.value`/`self._answers` values, DTMF digits, `Directive.reason`/`clarify` free text, LLM prompt/response content, chat context. If a task below seems to need something outside this list, stop and flag it — don't add it ad hoc.
- **Error isolation (design §7):** every attribute-setting call gets its own narrow `try/except Exception as exc: logger.warning(..., type(exc).__name__)`. Never let a tracing failure raise into — or rely on being caught by — the surrounding business-logic exception handler; a bug in tracing code must never be able to silently skip the real handoff/dispatch/speech call that follows it.
- **No bare `except`:** never swallow `asyncio.CancelledError`.
- **Existing test style:** `test_plan_runtime.py` uses explicit `@pytest.mark.asyncio`; `test_observer.py`, `test_health_observer.py`, and `test_queue_dispatcher.py` rely on `asyncio_mode = "auto"` (pyproject.toml) and omit the decorator. Match whichever file you're editing.
- **Verification gate:** after every task, run `just check` (ruff + mypy + pytest) from `vera-backend/` before moving on.

---

## Task 1: Shared OTel test-tracer infrastructure

Every later task's tests need a way to assert on real span names/attributes. `opentelemetry.trace.set_tracer_provider()` is a process-global, **one-shot** call — the first call in the whole process wins; every call after that is silently ignored (with a warning log), no matter what. This repo's test suite runs as ONE pytest session across two separate `testpaths` roots (`tests/` and `apps/agent_worker/tests/`, per `pyproject.toml:88`) whose `conftest.py` files don't share fixtures.

There's an existing landmine here: `tests/unit/observability/test_otel_auth.py` already calls `configure_observability()` (which itself calls `trace.set_tracer_provider(...)`) from several of its tests, and has its own `autouse=True` fixture that tries to reset the provider after each test. Verified by reading that file: none of its assertions depend on whether `set_tracer_provider` actually took global effect (they only inspect the *returned* `TracerProvider` object's own attributes), so it's safe to let our installer win the one-shot race — but we MUST make sure our installer runs *before* that file's tests get a chance to call `configure_observability` for real, or every later task's span assertions would silently see nothing (our own installer call would be the one that's ignored, since `test_otel_auth.py`'s internal call would have already won the race). A plain per-test fixture isn't enough to guarantee this — pytest's file collection order (`test_otel_auth.py` sorts before our new `test_otel_testing.py` in the same directory) could let `test_otel_auth.py` run first. The fix: install the provider via a **session-scoped, autouse** fixture, which pytest instantiates before the very first test in the whole session runs — regardless of which file that is.

**Files:**
- Create: `packages/vera_core/src/vera_core/observability/otel_testing.py`
- Modify: `apps/agent_worker/tests/unit/conftest.py`
- Create: `vera-backend/tests/unit/conftest.py`
- Test: `vera-backend/tests/unit/observability/test_otel_testing.py` (new — matches the existing sibling files `test_correlation.py`/`test_otel_auth.py` in that directory; this is the real, `testpaths`-collected location — `packages/vera_core/tests/` exists but is NOT in `pyproject.toml`'s `testpaths`, so a test placed there would never run under `just check`)

**Interfaces:**
- Produces: `vera_core.observability.otel_testing.install_test_tracer_provider() -> InMemorySpanExporter` (idempotent — safe to call from both conftest trees) and a pytest fixture named `otel_spans` (in both conftest.py files) that yields the shared `InMemorySpanExporter`, cleared before and after each test. A second fixture, `_install_test_tracer_provider` (session-scoped, autouse, defined once per conftest tree), is what actually wins the global one-shot race early; `otel_spans` depends on it.

- [ ] **Step 1: Write the failing test**

Create `vera-backend/tests/unit/observability/test_otel_testing.py` (the directory already exists with `test_correlation.py`/`test_otel_auth.py` as siblings):

```python
"""The shared test-tracer-provider installer used by the otel_spans fixture."""

from opentelemetry import trace

from vera_core.observability.otel_testing import install_test_tracer_provider


def test_install_is_idempotent_and_captures_spans() -> None:
    exporter = install_test_tracer_provider()
    exporter.clear()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe"):
        pass
    names = [span.name for span in exporter.get_finished_spans()]
    assert "probe" in names

    # Calling again must return the SAME exporter (no second TracerProvider install)
    again = install_test_tracer_provider()
    assert again is exporter
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_otel_testing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vera_core.observability.otel_testing'`

- [ ] **Step 3: Write minimal implementation**

Create `packages/vera_core/src/vera_core/observability/otel_testing.py`:

```python
"""Test-only OTel tracer provider installer.

`opentelemetry.trace.set_tracer_provider()` is a process-global, one-shot call — the first
call anywhere in the process wins; every call after that is silently ignored (with a warning),
including this file's own on a second invocation. This repo's test suite runs
`apps/agent_worker/tests` and `tests/` in one pytest session (`pyproject.toml` testpaths) whose
`conftest.py` files don't share fixtures, so both trees call `install_test_tracer_provider()`
from their own conftest; the module-level guard below makes that safe and gives both the SAME
exporter regardless of which tree's fixture runs first. Callers MUST invoke this from a
session-scoped autouse fixture (see conftest.py in both trees) rather than a plain per-test
fixture — otherwise an unrelated test that calls the real `configure_observability()` (e.g.
`tests/unit/observability/test_otel_auth.py`) could win the one-shot race first if it happens
to run before ours, silently discarding every span this exporter would otherwise have captured.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter = InMemorySpanExporter()
_installed = False


def install_test_tracer_provider() -> InMemorySpanExporter:
    """Install the shared test TracerProvider on first call; a no-op (returning the
    same exporter) on every call after that."""
    global _installed
    if not _installed:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
        _installed = True
    return _exporter
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/observability/test_otel_testing.py -v`
Expected: PASS

- [ ] **Step 5: Wire the fixtures into both conftest.py trees**

Modify `apps/agent_worker/tests/unit/conftest.py` — add to the existing file (keep `chat_ctx_texts` as-is):

```python
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


@pytest.fixture(scope="session", autouse=True)
def _install_test_tracer_provider() -> InMemorySpanExporter:
    # session-scoped + autouse: pytest instantiates this before the FIRST test in the whole
    # session runs, regardless of which file that is — see otel_testing.py's docstring for why
    # that matters (winning the global set_tracer_provider one-shot race).
    return install_test_tracer_provider()


@pytest.fixture
def otel_spans(
    _install_test_tracer_provider: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    """Cleared before and after each test; every test gets a clean span list even
    though the underlying TracerProvider is process-global and installed once."""
    _install_test_tracer_provider.clear()
    yield _install_test_tracer_provider
    _install_test_tracer_provider.clear()
```

Create `vera-backend/tests/unit/conftest.py` (this directory has no conftest.py yet — verify with `ls vera-backend/tests/unit/` before creating) with the identical fixtures (same content as above — it's the same idempotent installer function, so both trees end up sharing the one real exporter regardless of which tree's session-scoped fixture actually wins the race):

```python
"""Shared fixtures for tests/unit/ (vera_core + control_plane)."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vera_core.observability.otel_testing import install_test_tracer_provider


@pytest.fixture(scope="session", autouse=True)
def _install_test_tracer_provider() -> InMemorySpanExporter:
    return install_test_tracer_provider()


@pytest.fixture
def otel_spans(
    _install_test_tracer_provider: InMemorySpanExporter,
) -> Iterator[InMemorySpanExporter]:
    _install_test_tracer_provider.clear()
    yield _install_test_tracer_provider
    _install_test_tracer_provider.clear()
```

- [ ] **Step 6: Run the full test suite to confirm no collision**

Run: `cd vera-backend && just check`
Expected: PASS — in particular `tests/unit/observability/test_otel_auth.py` must still pass; this proves its internal `configure_observability()` calls losing the one-shot race (to our autouse fixture, which now always runs first) doesn't break its assertions, since they only inspect the returned `TracerProvider` object, never the global one.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/observability/otel_testing.py \
        tests/unit/observability/test_otel_testing.py \
        apps/agent_worker/tests/unit/conftest.py \
        tests/unit/conftest.py
git commit -m "test: add shared OTel test-tracer fixture for span assertions"
```

---

## Task 2: `Agent.id` fix — dynamic per-instance task identity

Foundational: every later handoff attribute reads `.id` off the successor/target agent object. Without this, every `PlanTaskAgent` instance collapses to the same class-derived label.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:84-94` (`PlanTaskAgent.__init__`), `:131-141` (`WrapUpAgent.__init__`)
- Modify: `apps/agent_worker/src/agent_worker/ivr_agent.py:138-163` (`IvrNavigatorAgent.__init__`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`, `apps/agent_worker/tests/unit/test_ivr_agent.py` (new file — none exists yet)

**Interfaces:**
- Produces: every `controller.agents[i].id == controller.plan.tasks[i].task_key`; `controller.wrap_up_agent.id == WRAP_UP_TASK_KEY` (`"@wrap_up"`, already defined at `plan_runtime.py:45`); a new `IVR_NAVIGATOR_ID = "@ivr_navigator"` constant exported from `agent_worker.ivr_agent`, and `IvrNavigatorAgent(...).id == IVR_NAVIGATOR_ID`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/agent_worker/tests/unit/test_plan_runtime.py`, inside `class TestConstruction`:

```python
    def test_task_agents_get_their_schema_task_key_as_id(self) -> None:
        controller, _ = _controller()
        assert [a.id for a in controller.agents] == ["intro_task", "gated_task", "last_task"]

    def test_wrap_up_agent_id_is_the_sentinel(self) -> None:
        controller, _ = _controller()
        assert controller.wrap_up_agent.id == WRAP_UP_TASK_KEY
```

(`WRAP_UP_TASK_KEY` is already imported at the top of this file.)

Create `apps/agent_worker/tests/unit/test_ivr_agent.py`:

```python
"""IVR navigator: id sentinel and the transfer-to-verification handoff."""

from livekit.agents import Agent

from agent_worker.ivr_agent import IVR_NAVIGATOR_ID, IvrNavigatorAgent


def _navigator(verifier: Agent) -> IvrNavigatorAgent:
    return IvrNavigatorAgent(verification_agent_factory=lambda: verifier)


class TestConstruction:
    def test_navigator_id_is_the_sentinel(self) -> None:
        navigator = _navigator(Agent(instructions="verify"))
        assert navigator.id == IVR_NAVIGATOR_ID
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestConstruction -v`
Expected: FAIL — `assert [a.id for a in controller.agents] == [...]` fails because every id is currently `"plan_task_agent"`.

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_ivr_agent.py -v`
Expected: FAIL with `ImportError: cannot import name 'IVR_NAVIGATOR_ID'`

- [ ] **Step 3: Write minimal implementation**

In `plan_runtime.py`, `PlanTaskAgent.__init__` — change:

```python
    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        super().__init__(
            instructions=_instructions(
                controller.plan,
                f"# Current task: {self._task.title}\n{self._task.prompt}",
                extra_instructions=controller.extra_instructions,
            ),
        )
```

to:

```python
    def __init__(self, controller: "PlanRunController", task_index: int) -> None:
        self._controller = controller
        self._task_index = task_index
        self._task = controller.plan.tasks[task_index]
        super().__init__(
            instructions=_instructions(
                controller.plan,
                f"# Current task: {self._task.title}\n{self._task.prompt}",
                extra_instructions=controller.extra_instructions,
            ),
            id=self._task.task_key,
        )
```

In `plan_runtime.py`, `WrapUpAgent.__init__` — change:

```python
    def __init__(self, controller: "PlanRunController") -> None:
        self._controller = controller
        super().__init__(
            instructions=_instructions(
                controller.plan,
                "# Current task: Wrap up\n"
                "The verification is complete. Close the call politely and briefly; "
                "do not open new topics or re-ask anything.",
                extra_instructions=controller.extra_instructions,
            ),
        )
```

to:

```python
    def __init__(self, controller: "PlanRunController") -> None:
        self._controller = controller
        super().__init__(
            instructions=_instructions(
                controller.plan,
                "# Current task: Wrap up\n"
                "The verification is complete. Close the call politely and briefly; "
                "do not open new topics or re-ask anything.",
                extra_instructions=controller.extra_instructions,
            ),
            id=WRAP_UP_TASK_KEY,
        )
```

In `ivr_agent.py`, add the sentinel constant near the top (after the `logger = logging.getLogger(...)` line):

```python
# Fixed id: exactly one IvrNavigatorAgent instance exists per call, so a sentinel (not a
# per-instance value) is enough — matches the "@..." sentinel convention used for the plan
# runtime's WrapUpAgent (agent_worker.plan_runtime.WRAP_UP_TASK_KEY).
IVR_NAVIGATOR_ID = "@ivr_navigator"
```

Then in `IvrNavigatorAgent.__init__`, change:

```python
        super().__init__(
            instructions=build_ivr_instructions(playbook, context),
            tools=[],
            turn_handling=ivr_turn_handling(),
        )
```

to:

```python
        super().__init__(
            instructions=build_ivr_instructions(playbook, context),
            tools=[],
            turn_handling=ivr_turn_handling(),
            id=IVR_NAVIGATOR_ID,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestConstruction apps/agent_worker/tests/unit/test_ivr_agent.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS (this also confirms nothing else depended on the old generic id/label)

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/src/agent_worker/ivr_agent.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py \
        apps/agent_worker/tests/unit/test_ivr_agent.py
git commit -m "feat: derive Agent.id from schema task_key / fixed sentinels"
```

---

## Task 3: Task-entry span attribution

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:217-223` (`note_task_entered`/`note_wrap_up_entered`), imports at top
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`, `class TestOnEnter`

**Interfaces:**
- Consumes: `WRAP_UP_TASK_KEY` (already in this file, Task 2's `id=` fix)
- Produces: nothing new consumed by later tasks — this is a leaf instrumentation point

- [ ] **Step 1: Write the failing tests**

Add to `apps/agent_worker/tests/unit/test_plan_runtime.py`, inside `class TestOnEnter`:

```python
    @pytest.mark.asyncio
    async def test_on_enter_tags_the_current_span_with_task_identity(
        self, otel_spans: Any
    ) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[1]  # gated_task, index 1
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.task.key"] == "gated_task"
        assert span.attributes["vera.task.index"] == 1

    @pytest.mark.asyncio
    async def test_wrap_up_on_enter_tags_the_sentinel(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.wrap_up_agent
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.task.key"] == WRAP_UP_TASK_KEY
        assert "vera.task.index" not in span.attributes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestOnEnter -v`
Expected: FAIL — `span.attributes["vera.task.key"]` raises `KeyError` (attribute doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

At the top of `plan_runtime.py`, add the import (alongside the existing `from livekit.agents import Agent, AgentSession, llm`):

```python
from opentelemetry import trace
```

`plan_runtime.py` never opens its own span (every instrumentation point here and in Tasks 4/6
only tags the already-current ambient span via `trace.get_current_span()`) — do NOT add a
module-level `tracer = trace.get_tracer(__name__)` here, it would be unused/dead code.

Change `note_task_entered`/`note_wrap_up_entered`:

```python
    def note_task_entered(self, index: int) -> None:
        self.active_task_index = index
        self._write_cursor(self.plan.tasks[index].task_key)

    def note_wrap_up_entered(self) -> None:
        self.active_task_index = None
        self._write_cursor(WRAP_UP_TASK_KEY)
```

to:

```python
    def note_task_entered(self, index: int) -> None:
        self.active_task_index = index
        task_key = self.plan.tasks[index].task_key
        self._tag_task_entry(task_key, index)
        self._write_cursor(task_key)

    def note_wrap_up_entered(self) -> None:
        self.active_task_index = None
        self._tag_task_entry(WRAP_UP_TASK_KEY, None)
        self._write_cursor(WRAP_UP_TASK_KEY)

    def _tag_task_entry(self, task_key: str, index: int | None) -> None:
        try:
            attrs: dict[str, str | int] = {"vera.task.key": task_key}
            if index is not None:
                attrs["vera.task.index"] = index
            trace.get_current_span().set_attributes(attrs)
        except Exception as exc:
            logger.warning(
                "plan run %s: task-entry span tagging failed (%s)",
                self.room_name,
                type(exc).__name__,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestOnEnter -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "feat: tag current span with active task identity on entry"
```

---

## Task 4: Task-complete handoff attribution

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:111-123` (`PlanTaskAgent._task_complete`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`, `class TestHandoff`

**Interfaces:**
- Consumes: `successor.id` (Task 2), `tracer` module global (Task 3)

- [ ] **Step 1: Write the failing test**

Add to `apps/agent_worker/tests/unit/test_plan_runtime.py`, inside `class TestHandoff`:

```python
    @pytest.mark.asyncio
    async def test_task_complete_tags_the_handoff_span(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[0]
        tracer = trace.get_tracer("test")
        controller.update_answers({"sections.a.in_network": "Yes"})
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await _tool(agent, "task_complete")()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.from_task"] == "intro_task"
        assert span.attributes["vera.handoff.to_task"] == "gated_task"
        assert span.attributes["vera.handoff.reason"] == "task_complete"

    @pytest.mark.asyncio
    async def test_task_complete_to_wrap_up_tags_the_sentinel(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        agent = controller.agents[2]  # last_task
        tracer = trace.get_tracer("test")
        with _session_patch(agent, MagicMock()), tracer.start_as_current_span("probe"):
            await _tool(agent, "task_complete")()
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.to_task"] == WRAP_UP_TASK_KEY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestHandoff -v`
Expected: FAIL — `KeyError: 'vera.handoff.from_task'`

- [ ] **Step 3: Write minimal implementation**

Change `PlanTaskAgent._task_complete`:

```python
    async def _task_complete(self) -> Agent | str:
        if takeover_engaged(self.session):
            # A str is a tool result, so the plan parks here. Returning `self` would
            # re-fire on_enter and speak the intro again.
            return "A human supervisor has taken over this call. Stay silent."
        if self._task.outro:
            # Exit speech first; LiveKit drains queued speech through the swap.
            self.session.say(self._task.outro)
        successor = await self._controller.advance_from(self._task_index)
        # Carry the call so far into the successor — LiveKit doesn't for a
        # tool-returned agent, so without this it re-greets and re-asks.
        await carry_chat_ctx(self, successor)
        return successor
```

to:

```python
    async def _task_complete(self) -> Agent | str:
        if takeover_engaged(self.session):
            # A str is a tool result, so the plan parks here. Returning `self` would
            # re-fire on_enter and speak the intro again.
            return "A human supervisor has taken over this call. Stay silent."
        if self._task.outro:
            # Exit speech first; LiveKit drains queued speech through the swap.
            self.session.say(self._task.outro)
        successor = await self._controller.advance_from(self._task_index)
        # Carry the call so far into the successor — LiveKit doesn't for a
        # tool-returned agent, so without this it re-greets and re-asks.
        await carry_chat_ctx(self, successor)
        self._tag_task_complete_handoff(successor)
        return successor

    def _tag_task_complete_handoff(self, successor: Agent) -> None:
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": self._task.task_key,
                    "vera.handoff.to_task": successor.id,
                    "vera.handoff.reason": "task_complete",
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: task-complete handoff span tagging failed (%s)",
                self._controller.room_name,
                type(exc).__name__,
            )
        logger.info(
            "handoff: %s -> %s (reason=task_complete)", self._task.task_key, successor.id
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestHandoff -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "feat: tag task-complete handoffs with from/to/reason attributes"
```

---

## Task 5: IVR→verification handoff attribution

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/ivr_agent.py:214-224` (`transfer_to_verification`)
- Test: `apps/agent_worker/tests/unit/test_ivr_agent.py`

**Interfaces:**
- Consumes: `IVR_NAVIGATOR_ID` (Task 2), verifier's `.id` (whatever `verification_agent_factory` returns — a `PlanTaskAgent` with Task 2's schema-derived id, or a `VoiceLabAgent` with its class-derived default id)

- [ ] **Step 1: Write the failing test**

Add to `apps/agent_worker/tests/unit/test_ivr_agent.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace


def _session_patch(agent: Agent, mock_session: MagicMock) -> Any:
    return patch.object(type(agent), "session", new=property(lambda self: mock_session))


class TestHandoff:
    @pytest.mark.asyncio
    async def test_transfer_tags_the_handoff_span(self, otel_spans: Any) -> None:
        verifier = Agent(instructions="verify", id="intro_task")
        navigator = _navigator(verifier)
        tracer = trace.get_tracer("test")
        mock_session = MagicMock()
        mock_session.interrupt = AsyncMock()
        with _session_patch(navigator, mock_session), tracer.start_as_current_span("probe"):
            handoff = await navigator.transfer_to_verification()
        assert handoff is verifier
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.from_task"] == IVR_NAVIGATOR_ID
        assert span.attributes["vera.handoff.to_task"] == "intro_task"
        assert span.attributes["vera.handoff.reason"] == "ivr_live_human"
```

Add the matching imports at the top of the file: `from typing import Any` alongside the existing imports, and `Agent` is already imported.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_ivr_agent.py::TestHandoff -v`
Expected: FAIL — `KeyError: 'vera.handoff.from_task'`

- [ ] **Step 3: Write minimal implementation**

At the top of `ivr_agent.py`, add:

```python
from opentelemetry import trace
```

near the other imports. `transfer_to_verification` only tags the already-current ambient span
(`trace.get_current_span()`) — it never opens its own, so do NOT add a module-level
`tracer = trace.get_tracer(__name__)` here either.

Change `transfer_to_verification`:

```python
    @function_tool
    async def transfer_to_verification(self) -> Agent:
        """Hand the call to the verification agent. Call this ONLY when a live human
        representative has clearly greeted you — a personal name paired with an open request
        for your info (e.g. "Hi, this is Martha, who am I speaking with?")."""
        logger.info("handoff: IVR navigator -> verification agent")
        verifier = self._make_verification_agent()
        # Carry the IVR conversation (incl. the member ID already spoken) into the
        # plan agent so it doesn't re-ask what the navigator already established.
        await carry_chat_ctx(self, verifier)
        return verifier
```

to:

```python
    @function_tool
    async def transfer_to_verification(self) -> Agent:
        """Hand the call to the verification agent. Call this ONLY when a live human
        representative has clearly greeted you — a personal name paired with an open request
        for your info (e.g. "Hi, this is Martha, who am I speaking with?")."""
        verifier = self._make_verification_agent()
        # Carry the IVR conversation (incl. the member ID already spoken) into the
        # plan agent so it doesn't re-ask what the navigator already established.
        await carry_chat_ctx(self, verifier)
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": IVR_NAVIGATOR_ID,
                    "vera.handoff.to_task": verifier.id,
                    "vera.handoff.reason": "ivr_live_human",
                }
            )
        except Exception as exc:
            logger.warning("IVR handoff span tagging failed (%s)", type(exc).__name__)
        logger.info(
            "handoff: %s -> %s (reason=ivr_live_human)", IVR_NAVIGATOR_ID, verifier.id
        )
        return verifier
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_ivr_agent.py::TestHandoff -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent_worker/src/agent_worker/ivr_agent.py apps/agent_worker/tests/unit/test_ivr_agent.py
git commit -m "feat: tag IVR-to-verification handoff with structured span attributes"
```

---

## Task 6: Rule-engine evaluation + forced-handoff span

This is the one component where Vera opens its own span rather than tagging an ambient one — `ObserverManager._record` runs from the Observer's background tail task, not inside any LiveKit-owned span. The span is opened in `observer.py` around the evaluate+apply sequence; `plan_runtime.py`'s `apply_directive_now` tags that SAME (ambient, inherited-via-context) span with the handoff attributes once it resolves the target — it does not open a second span. This matters for the test split below: `test_observer.py` (using the existing `FakeController`) can only prove the `fired`/`directive_type`/`rule_key` attributes; `test_plan_runtime.py` (using the real `PlanRunController`) proves the `from_task`/`to_task`/`reason` attributes land correctly once a `target` resolves, using a synthetic ambient span in place of the real one `observer.py` would provide in production.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py:419-423` (`ObserverManager._record`), imports at top
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:238-265` (`apply_directive_now`)
- Test: `apps/agent_worker/tests/unit/test_observer.py`, `class TestRuleIntervention`; `apps/agent_worker/tests/unit/test_plan_runtime.py`, `class TestDirectiveIntervention`

**Interfaces:**
- Consumes: `Directive`/`Terminate`/`SkipToTask`/`ReAsk` (`agent_worker.directives`, unchanged), `_directive_target` (`plan_runtime.py`, unchanged)
- Produces: span `vera.rule_engine.evaluate` with `vera.rule_engine.fired` always set; when a directive fires, `vera.handoff.directive_type` + `vera.handoff.rule_key` always, and (only for `Terminate`/`SkipToTask`) `vera.handoff.from_task`/`to_task`/`reason="flow_rule"`

- [ ] **Step 1: Write the failing test in test_observer.py**

Add to `apps/agent_worker/tests/unit/test_observer.py`, inside `class TestRuleIntervention`:

```python
    async def test_fired_rule_tags_the_evaluate_span(self, otel_spans: Any) -> None:
        flow = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.x", op="eq", value="No"),
            action="terminate_call",
        )
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "No", 90)])
        manager, _, _, controller = _manager(_plan(flow_rules=[flow]), extractor)
        await _feed(manager, _rep("the answer is no"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.rule_engine.evaluate"
        )
        assert span.attributes["vera.rule_engine.fired"] is True
        assert span.attributes["vera.handoff.directive_type"] == "Terminate"
        assert span.attributes["vera.handoff.rule_key"] == "stop"
        # PHI guardrail (design §6/§8): the answer value that fired this rule ("No") must
        # never appear in any attribute on this span — only the enum/key metadata above.
        assert "No" not in span.attributes.values()

    async def test_non_firing_evaluation_is_still_visible(self, otel_spans: Any) -> None:
        flow = FlowRule(
            rule_key="stop",
            when=Comparison(field="sections.a.x", op="eq", value="No"),
            action="terminate_call",
        )
        extractor = FakeExtractor([ExtractedAnswer("sections.a.x", "Yes", 90)])
        manager, _, _, controller = _manager(_plan(flow_rules=[flow]), extractor)
        await _feed(manager, _rep("the answer is yes"))
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.rule_engine.evaluate"
        )
        assert span.attributes["vera.rule_engine.fired"] is False
        assert "vera.handoff.directive_type" not in span.attributes
```

Add `from typing import Any` to this file's imports if not already present (it is — `from typing import Any` is already imported at the top).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_observer.py::TestRuleIntervention -v`
Expected: FAIL — `StopIteration` (no span named `vera.rule_engine.evaluate` exists yet)

- [ ] **Step 3: Write minimal implementation in observer.py**

At the top of `observer.py`, add the import (alongside the existing imports):

```python
from opentelemetry import trace
```

And a module-level tracer near `logger = logging.getLogger("agent_worker")`:

```python
tracer = trace.get_tracer(__name__)
```

Change the tail of `ObserverManager._record`:

```python
        # Mark dedup only after the write+emit land, so a failed emit is retried on the
        # next pass (the CP consumer is idempotent under the redelivery).
        self._answers[answer.field_path] = answer.value
        self._controller.update_answers(self._answers)
        directive = self._rule_engine.evaluate(self._answers)
        if directive is not None:
            # Redirect the live call NOW: interrupt the bot + swap/re-ask (the controller
            # serializes it against an in-flight task_complete handoff).
            await self._controller.apply_directive_now(directive)
```

to:

```python
        # Mark dedup only after the write+emit land, so a failed emit is retried on the
        # next pass (the CP consumer is idempotent under the redelivery).
        self._answers[answer.field_path] = answer.value
        self._controller.update_answers(self._answers)
        with tracer.start_as_current_span("vera.rule_engine.evaluate") as span:
            directive = self._rule_engine.evaluate(self._answers)
            try:
                span.set_attribute("vera.rule_engine.fired", directive is not None)
                if directive is not None:
                    span.set_attribute("vera.handoff.directive_type", type(directive).__name__)
                    span.set_attribute("vera.handoff.rule_key", directive.rule_key)
            except Exception as exc:
                logger.warning(
                    "observer manager %s: rule-engine span tagging failed (%s)",
                    self._room,
                    type(exc).__name__,
                )
            if directive is not None:
                # Redirect the live call NOW: interrupt the bot + swap/re-ask (the controller
                # serializes it against an in-flight task_complete handoff).
                await self._controller.apply_directive_now(directive)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_observer.py::TestRuleIntervention -v`
Expected: PASS

- [ ] **Step 5: Write the failing tests in test_plan_runtime.py**

Add to `apps/agent_worker/tests/unit/test_plan_runtime.py`, inside `class TestDirectiveIntervention`:

```python
    @pytest.mark.asyncio
    async def test_terminate_tags_the_ambient_span_with_handoff_attrs(
        self, otel_spans: Any
    ) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(Terminate(rule_key="not_covered"))
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.from_task"] == "intro_task"
        assert span.attributes["vera.handoff.to_task"] == WRAP_UP_TASK_KEY
        assert span.attributes["vera.handoff.reason"] == "flow_rule"

    @pytest.mark.asyncio
    async def test_skip_forward_tags_the_ambient_span(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(
                SkipToTask(rule_key="jump", task_key="last_task")
            )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert span.attributes["vera.handoff.to_task"] == "last_task"

    @pytest.mark.asyncio
    async def test_reask_does_not_tag_handoff_attrs(self, otel_spans: Any) -> None:
        from opentelemetry import trace

        controller, _ = _controller()
        controller.note_task_entered(0)
        _session, _order = _attach_ordered_session(controller)
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("probe"):
            await controller.apply_directive_now(
                ReAsk(rule_key="ded", reason="Deductible was stated twice.")
            )
        span = next(s for s in otel_spans.get_finished_spans() if s.name == "probe")
        assert "vera.handoff.from_task" not in span.attributes
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest "apps/agent_worker/tests/unit/test_plan_runtime.py::TestDirectiveIntervention::test_terminate_tags_the_ambient_span_with_handoff_attrs" "apps/agent_worker/tests/unit/test_plan_runtime.py::TestDirectiveIntervention::test_skip_forward_tags_the_ambient_span" -v`
Expected: FAIL — `KeyError: 'vera.handoff.from_task'`

- [ ] **Step 7: Write minimal implementation in plan_runtime.py**

Change `apply_directive_now`:

```python
        try:
            async with self.lock:
                if self.active_task_index is None:
                    return
                if isinstance(directive, ReAsk):
                    await self._session.interrupt()
                    self._session.generate_reply(instructions=self._reask_instruction(directive))
                    return
                target = self._directive_target(directive)
                if target is None:  # skip whose target is already at/behind us → no-op
                    return
                await self._session.interrupt()
                self._session.update_agent(target)
        except Exception as exc:
```

to:

```python
        try:
            async with self.lock:
                if self.active_task_index is None:
                    return
                if isinstance(directive, ReAsk):
                    await self._session.interrupt()
                    self._session.generate_reply(instructions=self._reask_instruction(directive))
                    return
                target = self._directive_target(directive)
                if target is None:  # skip whose target is already at/behind us → no-op
                    return
                self._tag_rule_handoff(target)
                await self._session.interrupt()
                self._session.update_agent(target)
        except Exception as exc:
```

And add the new method near `_directive_target`:

```python
    def _tag_rule_handoff(self, target: Agent) -> None:
        try:
            trace.get_current_span().set_attributes(
                {
                    "vera.handoff.from_task": self.plan.tasks[cast(int, self.active_task_index)].task_key,
                    "vera.handoff.to_task": target.id,
                    "vera.handoff.reason": "flow_rule",
                }
            )
        except Exception as exc:
            logger.warning(
                "plan run %s: rule-handoff span tagging failed (%s)",
                self.room_name,
                type(exc).__name__,
            )
```

`self.active_task_index` is `int | None` but is guaranteed `int` here (the `if self.active_task_index is None: return` guard above already returned) — `cast` needs `from typing import cast` at the top of `plan_runtime.py` (add it if not already imported; check the existing `from typing import Any` line and extend it to `from typing import Any, cast`).

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestDirectiveIntervention -v`
Expected: PASS (all tests in the class, including the 3 new ones and the pre-existing ones — this proves the new `_tag_rule_handoff` call doesn't disturb the existing interrupt/swap ordering assertions)

- [ ] **Step 9: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/test_observer.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "feat: instrument rule-engine evaluation and forced handoffs"
```

---

## Task 7: LLM-call purpose tagging (observer extraction + health observer)

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py:102-106` (`ResilientAnswerExtractor.extract`)
- Modify: `apps/agent_worker/src/agent_worker/health_observer.py:129-136` (`CallHealthObserver._analyze_once`), imports at top
- Test: `apps/agent_worker/tests/unit/test_observer.py`, `class TestResilientExtractor`; `vera-backend/tests/unit/worker/test_health_observer.py`

**Interfaces:**
- Consumes: `tracer` module global (already added to `observer.py` in Task 6; new in `health_observer.py`)
- Produces: span `vera.observer.extraction_llm_call` (attrs `vera.llm.purpose="observer_extraction"`, `vera.task.key`); span `vera.health_observer.llm_call` (attr `vera.llm.purpose="health_observer"`) — both wrap only the `ResilientLLM.complete()` call, nothing else

- [ ] **Step 1: Write the failing test for the extractor**

Add to `apps/agent_worker/tests/unit/test_observer.py`, inside `class TestResilientExtractor`:

```python
    async def test_extract_tags_the_llm_call_span(self, otel_spans: Any) -> None:
        reply = '[{"field_path": "sections.a.x", "value": "Yes", "confidence": 90}]'
        llm = FakeCompletionLLM(reply)
        await ResilientAnswerExtractor(llm).extract(_plan().tasks[0], "Representative: yes")
        span = next(
            s
            for s in otel_spans.get_finished_spans()
            if s.name == "vera.observer.extraction_llm_call"
        )
        assert span.attributes["vera.llm.purpose"] == "observer_extraction"
        assert span.attributes["vera.task.key"] == "t1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest "apps/agent_worker/tests/unit/test_observer.py::TestResilientExtractor::test_extract_tags_the_llm_call_span" -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 3: Implement in observer.py**

Change `ResilientAnswerExtractor.extract`:

```python
    async def extract(self, task: PlanTask, transcript: str) -> list[ExtractedAnswer]:
        # A whole-chain outage PROPAGATES rather than returning [], which is indistinguishable
        # from "the rep answered nothing" and would retire those turns unextracted.
        reply = await self._llm.complete(system=_extraction_instructions(task), user=transcript)
        return _parse_extraction(reply)
```

to:

```python
    async def extract(self, task: PlanTask, transcript: str) -> list[ExtractedAnswer]:
        # A whole-chain outage PROPAGATES rather than returning [], which is indistinguishable
        # from "the rep answered nothing" and would retire those turns unextracted.
        with tracer.start_as_current_span(
            "vera.observer.extraction_llm_call",
            attributes={"vera.llm.purpose": "observer_extraction", "vera.task.key": task.task_key},
        ):
            reply = await self._llm.complete(system=_extraction_instructions(task), user=transcript)
        return _parse_extraction(reply)
```

(`tracer` already exists at module level in `observer.py` from Task 6.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest "apps/agent_worker/tests/unit/test_observer.py::TestResilientExtractor::test_extract_tags_the_llm_call_span" -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the health observer**

Read `vera-backend/tests/unit/worker/test_health_observer.py` first to match its exact `_observer(...)`/`_feed(...)` helper signatures, then add:

```python
    async def test_analysis_tags_the_llm_call_span(otel_spans: Any) -> None:
        llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
        obs = _observer(llm, stream=stream, bus=bus)
        await _feed(obs, ("agent", "Question?"), ("user", "Yes it's covered."))
        span = next(
            s
            for s in otel_spans.get_finished_spans()
            if s.name == "vera.health_observer.llm_call"
        )
        assert span.attributes["vera.llm.purpose"] == "health_observer"
```

(Match the exact keyword arguments `_observer` takes — read the file's existing tests, e.g. `test_analyzes_after_min_user_turns_and_emits_both_rails`, and copy its call shape exactly rather than guessing.)

- [ ] **Step 6: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest "tests/unit/worker/test_health_observer.py::test_analysis_tags_the_llm_call_span" -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 7: Implement in health_observer.py**

At the top of `health_observer.py`, add the import (alongside the existing imports):

```python
from opentelemetry import trace
```

And a module-level tracer near `logger = logging.getLogger("agent_worker")`:

```python
tracer = trace.get_tracer(__name__)
```

Change `_analyze_once`:

```python
    async def _analyze_once(self) -> None:
        user_message = self._transcript.render_user_message()
        turn_count = self._transcript.turn_count
        try:
            reply = await self._llm.complete(system=HEALTH_SYSTEM_PROMPT, user=user_message)
        except Exception as exc:  # prompt/reply are PHI — type name only
            logger.warning("health analysis for %s skipped (%s)", self._room, type(exc).__name__)
            return
```

to:

```python
    async def _analyze_once(self) -> None:
        user_message = self._transcript.render_user_message()
        turn_count = self._transcript.turn_count
        try:
            with tracer.start_as_current_span(
                "vera.health_observer.llm_call",
                attributes={"vera.llm.purpose": "health_observer"},
            ):
                reply = await self._llm.complete(system=HEALTH_SYSTEM_PROMPT, user=user_message)
        except Exception as exc:  # prompt/reply are PHI — type name only
            logger.warning("health analysis for %s skipped (%s)", self._room, type(exc).__name__)
            return
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest "tests/unit/worker/test_health_observer.py::test_analysis_tags_the_llm_call_span" -v`
Expected: PASS

- [ ] **Step 9: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py \
        apps/agent_worker/src/agent_worker/health_observer.py \
        apps/agent_worker/tests/unit/test_observer.py \
        tests/unit/worker/test_health_observer.py
git commit -m "feat: tag observer-extraction and health-observer LLM calls by purpose"
```

---

## Task 8: Control-plane dispatch spans

Three spans, not one — see spec §5.7. `compile_call_plan` (schema-scoped, memoized per pass) and `PrefillFuser.fuse` (form-scoped) both run **before** the `Call` row — and thus `room_name` — exists in the dispatch loop, so neither can carry `call_trace_attributes`. Only the new `vera.dispatch.stage_call` span, wrapping the section where `room_name` is actually in scope, correlates into the call's Langfuse trace.

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py:587-643` (`_resolve_plan_template`), `:555-584` (`_resolve_call_plan`), `:~393-474` (the dispatch loop's savepoint block), imports at top
- Test: `vera-backend/tests/unit/services/test_queue_dispatcher.py`, `class TestCallPlanStaging`

**Interfaces:**
- Consumes: `call_trace_attributes` (`vera_core.observability.correlation`, already used by the agent worker)
- Produces: spans `vera.dispatch.compile_plan` (`vera.dispatch.schema_version`), `vera.dispatch.fuse_plan` (`vera.dispatch.form_id`), `vera.dispatch.stage_call` (`call_trace_attributes(room_name)` + `vera.dispatch.ivr_enabled` + `vera.dispatch.task_count`)

- [ ] **Step 1: Write the failing tests**

Read `packages/vera_core/src/vera_core/services/queue_dispatcher.py` fully around lines 380-475 first to get the exact current indentation of the savepoint block (this plan was drafted against a snapshot of the file; re-read it before editing since line numbers may have shifted from earlier tasks in other files — this task doesn't touch any file the earlier tasks touched, but always verify against the live file, never trust a plan's line numbers blindly).

Add to `vera-backend/tests/unit/services/test_queue_dispatcher.py`, inside `class TestCallPlanStaging`:

```python
    async def test_stage_call_span_carries_correlation_and_counts(
        self, _stub_credentials: dict[str, dict[str, Any] | None], otel_spans: Any
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        pv = _prompt_version(sv)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(
            tenant=tenant, candidates=[form], schema_version=sv, prompt_version=pv
        )
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        await _dispatch(session, tenant.id, livekit, plan_service=plans)

        room_name = livekit.created[0]
        span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.dispatch.stage_call"
        )
        assert span.attributes["vera.room"] == room_name
        assert span.attributes["vera.tenant_id"] == str(tenant.id)
        assert "vera.dispatch.task_count" in span.attributes
        assert span.attributes["vera.dispatch.ivr_enabled"] is False

    async def test_compile_and_fuse_spans_are_schema_and_form_scoped(
        self, _stub_credentials: dict[str, dict[str, Any] | None], otel_spans: Any
    ) -> None:
        tenant = _tenant()
        sv = _schema_version(IBV_SCHEMA_JSON)
        form = _form(tenant.id, schema_version_id=sv.id)
        session = FakeSession(tenant=tenant, candidates=[form], schema_version=sv)
        livekit = FakeLiveKit()
        plans = FakeCallPlanService()

        await _dispatch(session, tenant.id, livekit, plan_service=plans)

        names = [s.name for s in otel_spans.get_finished_spans()]
        compile_span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.dispatch.compile_plan"
        )
        fuse_span = next(
            s for s in otel_spans.get_finished_spans() if s.name == "vera.dispatch.fuse_plan"
        )
        assert compile_span.attributes["vera.dispatch.schema_version"] == str(sv.id)
        assert fuse_span.attributes["vera.dispatch.form_id"] == str(form.id)
        assert "vera.room" not in compile_span.attributes  # room_name doesn't exist yet here
        assert "vera.room" not in fuse_span.attributes
        assert "vera.dispatch.compile_plan" in names and "vera.dispatch.fuse_plan" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && uv run pytest "tests/unit/services/test_queue_dispatcher.py::TestCallPlanStaging::test_stage_call_span_carries_correlation_and_counts" "tests/unit/services/test_queue_dispatcher.py::TestCallPlanStaging::test_compile_and_fuse_spans_are_schema_and_form_scoped" -v`
Expected: FAIL — `StopIteration` on both (none of the three span names exist yet)

- [ ] **Step 3: Add the imports**

In `queue_dispatcher.py`, change:

```python
from vera_core.observability.correlation import room_name_for_call
```

to:

```python
from opentelemetry import trace

from vera_core.observability.correlation import call_trace_attributes, room_name_for_call
```

(keep this in the existing import-sorted position — run `ruff check --fix` after editing if the exact alphabetical placement is unclear; do not hand-guess import order across unrelated blocks).

Add a module-level tracer near the top-level `logger = logging.getLogger(...)` in this file:

```python
tracer = trace.get_tracer(__name__)
```

- [ ] **Step 4: Wrap `compile_call_plan` in `_resolve_plan_template`**

Change:

```python
            plan = compile_call_plan(
                doc,
                prompt_doc,
                schema_version_id=schema_version.id,
                prompt_version_id=prompt_version_id,
            )
            resolved = (PrefillFuser(doc, plan), prompt_version_id)
```

to:

```python
            with tracer.start_as_current_span(
                "vera.dispatch.compile_plan",
                attributes={"vera.dispatch.schema_version": str(schema_version.id)},
            ):
                plan = compile_call_plan(
                    doc,
                    prompt_doc,
                    schema_version_id=schema_version.id,
                    prompt_version_id=prompt_version_id,
                )
            resolved = (PrefillFuser(doc, plan), prompt_version_id)
```

- [ ] **Step 5: Wrap `fuser.fuse` in `_resolve_call_plan`**

Change:

```python
    try:
        values = await current_values_by_path(session, form.id)
        fused = fuser.fuse(values, current_year=datetime.now(_EASTERN).year)
        return fused, prompt_version_id
```

to:

```python
    try:
        values = await current_values_by_path(session, form.id)
        with tracer.start_as_current_span(
            "vera.dispatch.fuse_plan", attributes={"vera.dispatch.form_id": str(form.id)}
        ):
            fused = fuser.fuse(values, current_year=datetime.now(_EASTERN).year)
        return fused, prompt_version_id
```

- [ ] **Step 6: Wrap the room-creation section of the dispatch loop**

Re-read the current file at the dispatch loop's savepoint block before editing — match against this shape (it should be materially unchanged from what this plan was drafted against):

```python
                room_name = room_name_for_call(tenant_id, call.id)
                if plan_service is not None and staged_plan is not None:
                    plan, plan_prompt_version_id = staged_plan
                    # Fail fast: a staging failure aborts THIS dispatch — the raise
                    # propagates to the except below, which rolls back the Call and
                    # reverts the form to IN_QUEUE. Never place a call whose plan
                    # didn't reach the store (the plan-only worker can't serve it).
                    await plan_service.put(room_name, plan)
                    metadata["use_call_plan"] = True
                    staged_plan_room = room_name  # for orphan cleanup on rollback
                    # Lineage rides the same failure path as the put above: a
                    # staging raise aborts the dispatch, so a call never claims a
                    # prompt version it didn't actually load.
                    call.prompt_version_id = plan_prompt_version_id
                if form.ivr_navigation_enabled and provider is not None:
                    await add_active_playbook_metadata(session, provider.id, metadata)
                if form.ivr_navigation_enabled:
                    await add_agent_context_metadata(session, form, metadata)
                # Unlike the two calls above, this one never raises — a broken config-table
                # read degrades to the hardcoded default instead of failing the dispatch.
                await add_llm_model_override_metadata(session, metadata)
                try:
                    await livekit.create_call_room(room_name, metadata=metadata)
                except Exception as exc:
                    # metadata carries agent_context (raw PHI); a raised SDK/Twirp error could embed
                    # the request body, and the outer handler logs the traceback — re-raise PHI-free
                    # (chain suppressed) so no PHI can leak into logs.
                    raise LiveKitUnavailable(
                        f"create_call_room failed: {type(exc).__name__}"
                    ) from None
                session.add(
                    CallEvent(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.INITIATED.value,
                    )
                )

                if parent_call_id is not None:
                    session.add(
                        CallLineage(
                            tenant_id=tenant_id,
                            parent_call_id=parent_call_id,
                            retry_call_id=call.id,
                        )
                    )
```

Replace with (note: only the indentation of the existing body changes, by exactly one level; no line inside the `with` block is otherwise edited):

```python
                room_name = room_name_for_call(tenant_id, call.id)
                span_attrs: dict[str, Any] = dict(call_trace_attributes(room_name))
                span_attrs["vera.dispatch.ivr_enabled"] = bool(
                    metadata.get("enable_ivr_navigation")
                )
                if staged_plan is not None:
                    span_attrs["vera.dispatch.task_count"] = len(staged_plan[0].tasks)
                with tracer.start_as_current_span("vera.dispatch.stage_call", attributes=span_attrs):
                    if plan_service is not None and staged_plan is not None:
                        plan, plan_prompt_version_id = staged_plan
                        # Fail fast: a staging failure aborts THIS dispatch — the raise
                        # propagates to the except below, which rolls back the Call and
                        # reverts the form to IN_QUEUE. Never place a call whose plan
                        # didn't reach the store (the plan-only worker can't serve it).
                        await plan_service.put(room_name, plan)
                        metadata["use_call_plan"] = True
                        staged_plan_room = room_name  # for orphan cleanup on rollback
                        # Lineage rides the same failure path as the put above: a
                        # staging raise aborts the dispatch, so a call never claims a
                        # prompt version it didn't actually load.
                        call.prompt_version_id = plan_prompt_version_id
                    if form.ivr_navigation_enabled and provider is not None:
                        await add_active_playbook_metadata(session, provider.id, metadata)
                    if form.ivr_navigation_enabled:
                        await add_agent_context_metadata(session, form, metadata)
                    # Unlike the two calls above, this one never raises — a broken config-table
                    # read degrades to the hardcoded default instead of failing the dispatch.
                    await add_llm_model_override_metadata(session, metadata)
                    try:
                        await livekit.create_call_room(room_name, metadata=metadata)
                    except Exception as exc:
                        # metadata carries agent_context (raw PHI); a raised SDK/Twirp error could embed
                        # the request body, and the outer handler logs the traceback — re-raise PHI-free
                        # (chain suppressed) so no PHI can leak into logs.
                        raise LiveKitUnavailable(
                            f"create_call_room failed: {type(exc).__name__}"
                        ) from None
                    session.add(
                        CallEvent(
                            tenant_id=tenant_id,
                            call_id=call.id,
                            event_type=CallEventType.STATUS.value,
                            event_value=CallStatus.INITIATED.value,
                        )
                    )

                    if parent_call_id is not None:
                        session.add(
                            CallLineage(
                                tenant_id=tenant_id,
                                parent_call_id=parent_call_id,
                                retry_call_id=call.id,
                            )
                        )
```

`span_attrs: dict[str, Any]` needs `Any` imported — check the top of the file for an existing `from typing import ...` line and extend it rather than adding a duplicate import.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest "tests/unit/services/test_queue_dispatcher.py::TestCallPlanStaging" -v`
Expected: PASS — all tests in the class, including the pre-existing ones (proves the reindent didn't change behavior)

- [ ] **Step 8: Run the full suite**

Run: `cd vera-backend && just check`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        tests/unit/services/test_queue_dispatcher.py
git commit -m "feat: instrument control-plane dispatch with compile/fuse/stage spans"
```

---

## Final check

- [ ] **Run the full gate one more time from a clean tree**

```bash
cd vera-backend && just check
```

Expected: PASS. All 8 tasks' commits are on the branch; nothing left uncommitted.

- [ ] **Run the mandatory post-implementation simplify pass** (per repo-root `CLAUDE.md`)

Trigger **"simplify code"** (the `code-simplifier` agent) on the full diff since this plan's first commit, in the SAME session that did the implementation. After it applies any refinements, re-run `just check` and re-verify before considering this plan done.
