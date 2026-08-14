"""Simulation: owed-question handling over many answer states, for EVERY catalog schema.

Not a unit test of one function — a sweep, parametrized over `catalog.SCHEMAS`. Adding an
insurance type is "write a builder module, register it there" (see that module's docstring), so
a newly declared schema is swept by everything below without editing this file — which is the
point: the three bugs this file was written for were all authoring-shape bugs, and a new
catalog is where those land.

Only a schema's GATE-REFERENCED fields change the shape of the question tree, so that is the
axis the states are generated over, three ways:

* a **spoken-order walk** — walk each task's tree in order, answer one owed target per step
  under five answering policies, which is the shape a real call takes and the only strategy
  that whittles a fan-out down member by member;
* **seeded random sampling** over the gate-referenced leaves (fixed seed, printed on failure);
* **adversarial states derived from the document** — every routing branch taken / refused /
  inapplicable / left silent, every fan-out axis at one, half and all members answered, every
  authored either/or from each side and both, the `not_in` gate boundary literals, and every
  confirm-node state.

Nothing here is a hardcoded path or value: the routing sets, fan-out axes, either/or pairs,
confirm anchors, gate literals and even the gate-OPENING value per field are all read off the
document or the compiled tree. A schema that declares none of a given structure simply
generates none of those states, and the tests about that structure skip rather than pass
vacuously.

The invariants are asserted per (schema, task, state) and the failure path SHRINKS the state
that broke one — drop answers while the same check still fails — so a red run reports a minimal
reproducer rather than 150 paths. Every value here is synthetic; no PHI.

The worker half (`PlanRunController` and both completion guards) is the point of the file: its
ceilings and refusal budgets are the least-covered arithmetic on this path, and a ceiling that
disagrees with the list the agent was handed releases a guard with questions still unasked.
"""

import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import cache
from random import Random
from typing import Any
from unittest.mock import MagicMock

import pytest
from livekit.agents import Agent
from test_plan_runtime import (
    FakeObserverManager,
    FakeRunState,
    _fused_plan,
    _rep_turn,
    _session_patch,
    _stated_total,
    _tool,
)

from agent_worker.plan_runtime import (
    _GAP_FRUITLESS_REFUSALS,
    _REFUSAL_DRAIN_TIMEOUT_S,
    _TASK_FRUITLESS_REFUSALS,
    GapTaskAgent,
    PlanRunController,
)
from vera_core.forms.call_plan import (
    CallPlan,
    PlanFieldDescriptor,
    PlanTask,
    _exploded,
    focus_call_plan,
    focus_questions,
    owed_now,
)
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.conditions import (
    alternative_index,
    evaluate,
    has_value,
    is_applicable,
    is_required,
    leaf_gates,
)
from vera_core.forms.dsl import (
    AllCondition,
    AnyCondition,
    Comparison,
    Condition,
    FormSchemaDoc,
    Group,
    Leaf,
    NotCondition,
    RefCondition,
    condition_field_paths,
)
from vera_core.forms.prompting import (
    immediate_confirms_by_anchor,
    numbered_questions,
    render_digest,
    render_panels,
)
from vera_core.forms.question_plan import (
    PromptPanel,
    drop_questions,
    iter_questions,
    keep_questions,
)

Answers = Mapping[str, str]

_SYNTHETIC = {
    "text": "Some text",
    "enum": "Yes",
    "date": "1991-04-12",
    "currency": "$500",
    "percent": "20%",
    "integer": "3",
    "phone": "5551234567",
}


# -- one schema under simulation -----------------------------------------------------------


@dataclass(eq=False)  # identity equality, so the caches below can key on the object
class Schema:
    """One catalog schema, plus every projection of it the sweep needs.

    Built once per insurance type. Everything here is derived from the document or the compiled
    plan — never restated — so a catalog edit moves the simulation with it."""

    insurance_type: str
    doc: FormSchemaDoc
    plan: CallPlan
    shared: dict[str, Condition]
    leaves: dict[str, Leaf]
    descriptors: dict[str, PlanFieldDescriptor]
    task_index: dict[str, int]
    owner_title: dict[str, str | None]
    group_titles: set[str]
    alternatives: Mapping[str, tuple[str, ...]]
    # `{anchor path: [(collected path, rendered line)]}` for the `confirm_immediate` leaves.
    confirms_by_anchor: dict[str, list[tuple[str, str]]]
    confirm_paths: set[str]
    anchor_of: dict[str, str]
    # The task a confirm is SPOKEN in (`confirm_in_task` routing), or None with no confirms.
    confirm_task: PlanTask | None
    gate_literals: dict[str, list[str]]
    gate_refs: frozenset[str]
    candidates: dict[str, tuple[str, ...]]
    # Per gate-referenced path, the value satisfying the most / fewest gates that read it.
    opening: dict[str, str]
    closing: dict[str, str]
    question_text: dict[str, str]
    text_targets: dict[str, list[tuple[str, ...]]]

    def __str__(self) -> str:
        return self.insurance_type


def _gate_literals(doc: FormSchemaDoc, shared: Mapping[str, Condition]) -> dict[str, list[str]]:
    """`{path: every literal some gate compares it against}` — the values that flip tree shape.

    A leaf's own vocabulary is not enough: `ibv_standard`'s deductible gates are
    `not_in ["$0", "None", "No Deductible", "Unlimited", "No Limit"]` over a currency leaf with
    no vocabulary at all, so those boundary strings exist nowhere else in the document."""
    found: dict[str, list[str]] = {}

    def walk(cond: Condition) -> Iterator[Comparison]:
        if isinstance(cond, RefCondition):
            target = shared.get(cond.ref)
            if target is not None:
                yield from walk(target)
        elif isinstance(cond, AllCondition):
            for sub in cond.all:
                yield from walk(sub)
        elif isinstance(cond, AnyCondition):
            for sub in cond.any:
                yield from walk(sub)
        elif isinstance(cond, NotCondition):
            yield from walk(cond.not_)
        else:
            yield cond

    for _path, _leaf, gates in leaf_gates(doc):
        for gate in gates:
            for comparison in walk(gate):
                values = (
                    comparison.value if isinstance(comparison.value, list) else [comparison.value]
                )
                found.setdefault(comparison.field, []).extend(values)
    return {path: list(dict.fromkeys(values)) for path, values in found.items()}


def _candidates_for(
    path: str,
    field: Leaf | PlanFieldDescriptor,
    literals: Mapping[str, list[str]],
) -> tuple[str, ...]:
    """Every value worth recording at `path`: its vocabulary, the literals its gates test, and
    one synthetic fallback so a free-text leaf can still be answered.

    `default` is in because the intake UI materializes it, so it is a value the field really
    takes — the same set `intake.enum_accepted_values` accepts on the document side."""
    return tuple(
        dict.fromkeys(
            [
                *(field.values or []),
                *(field.special_values or []),
                *literals.get(path, []),
                *([field.default] if field.default else []),
                *([field.inapplicable_value] if field.inapplicable_value else []),
                _SYNTHETIC[field.type],
            ]
        )
    )


def _gate_extremes(
    doc: FormSchemaDoc,
    shared: Mapping[str, Condition],
    candidates: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Per gate-referenced path, the value that satisfies the MOST gates reading it and the one
    that satisfies the FEWEST — the schema's own "open everything" and "close everything".

    Derived by evaluating the real conditions rather than by naming values: a hardcoded "Yes" /
    "Family" / "Male" list is `ibv_standard` lore, and on any other schema it would quietly
    generate states that open no gates at all — a corpus that tests less while staying green."""
    reading: dict[str, list[Condition]] = {}
    for _path, _leaf, chain in leaf_gates(doc):
        for gate in chain:
            for ref in condition_field_paths(gate, shared):
                reading.setdefault(ref, []).append(gate)

    def score(path: str, value: str) -> int:
        # Judged with only this path set: the other conjuncts of an `all` are equally unmet for
        # every candidate, so the ranking between candidates still holds.
        return sum(evaluate(gate, {path: value}, shared) for gate in reading[path])

    opening: dict[str, str] = {}
    closing: dict[str, str] = {}
    for path in reading:
        options = candidates.get(path)
        if not options:
            continue  # a gate over a path this document has no leaf for
        opening[path] = max(options, key=lambda value: score(path, value))
        closing[path] = min(options, key=lambda value: score(path, value))
    return opening, closing


@cache
def _schema(insurance_type: str) -> Schema:
    build = SCHEMAS[insurance_type][1]
    doc = build()
    # Fused with an empty prefill: what the worker actually loads (slots resolved, no intake
    # values), and `gating_seed` is then empty so a state IS the answer map.
    plan = _fused_plan(build, {})
    shared = dict(doc.shared_conditions or {})
    leaves = dict(doc.leaf_items())
    descriptors = {field.path: field for task in plan.tasks for field in task.fields}
    literals = _gate_literals(doc, shared)
    candidates = {
        path: _candidates_for(path, descriptors.get(path) or leaf, literals)
        for path, leaf in leaves.items()
    }
    opening, closing = _gate_extremes(doc, shared, candidates)
    confirms = immediate_confirms_by_anchor(doc)
    confirm_paths = {path for pairs in confirms.values() for path, _line in pairs}
    question_text = {
        path: question.text
        for task in plan.tasks
        for question in iter_questions(task.panels)
        for path in question.target_paths
    }
    text_targets: dict[str, list[tuple[str, ...]]] = {}
    for task in plan.tasks:
        for question in iter_questions(task.panels):
            if question.target_paths:
                text_targets.setdefault(question.text, []).append(tuple(question.target_paths))
    return Schema(
        insurance_type=insurance_type,
        doc=doc,
        plan=plan,
        shared=shared,
        leaves=leaves,
        descriptors=descriptors,
        task_index={task.task_key: index for index, task in enumerate(plan.tasks)},
        owner_title={path: field.owner_title for path, field in descriptors.items()},
        # From the document, NOT from `owner_title`: `still_needed` is built out of those, so
        # checking it against them would be checking the compiler against itself.
        group_titles={
            field.title
            for _path, field in doc._iter_fields()
            if isinstance(field, Group) and field.title
        },
        alternatives=alternative_index(plan.alternative_pairs),
        confirms_by_anchor=confirms,
        confirm_paths=confirm_paths,
        anchor_of={path: anchor for anchor, pairs in confirms.items() for path, _line in pairs},
        confirm_task=next(
            (task for task in plan.tasks if any(f.path in confirm_paths for f in task.fields)),
            None,
        ),
        gate_literals=literals,
        # The paths come from production's own walk, so the sweep's main sampling axis does not
        # rest on `_gate_literals` (which exists only to reach the comparison VALUES). Every
        # gate-referenced LEAF, not just the collectable ones: a context-role gate reference can
        # gate a whole task (`ibv_standard`'s `spouse_gender` gates `male_partner`).
        gate_refs=frozenset(
            ref
            for _path, _leaf, gates in leaf_gates(doc)
            for gate in gates
            for ref in condition_field_paths(gate, shared)
        )
        & frozenset(leaves),
        candidates=candidates,
        opening=opening,
        closing=closing,
        question_text=question_text,
        text_targets=text_targets,
    )


@pytest.fixture(params=sorted(SCHEMAS))
def schema(request: pytest.FixtureRequest) -> Schema:
    """Every registered catalog schema, so a newly declared one joins the sweep on its own."""
    return _schema(str(request.param))


def test_every_registered_schema_is_swept() -> None:
    """The registry IS the parametrization, so this is the one place that would notice a schema
    that compiles but cannot be simulated at all."""
    assert set(SCHEMAS)
    for insurance_type in SCHEMAS:
        built = _schema(insurance_type)
        assert built.plan.tasks and built.descriptors
        assert built.gate_refs <= set(built.leaves)


# -- answer states ------------------------------------------------------------------------


@dataclass
class Case:
    label: str
    answers: dict[str, str]


def _pure_owed(s: Schema, task: PlanTask, answers: Answers) -> list[str]:
    """Applicable ∧ required ∧ nothing on file — the set invariant 1 says must all be asked.

    Wider than the worker's `gap_fields`, which also lets an answered either/or member satisfy
    its sibling; a question this set owes and the tree cannot render is silent data loss."""
    return [
        field.path
        for field in task.fields
        if is_applicable(field.gates, answers, s.shared)
        and is_required(field, answers, s.shared)
        and not has_value(answers, field.path)
    ]


def _pick(s: Schema, policy: str, path: str, rng: Random) -> str:
    """The value `policy` records at `path`."""
    options = s.candidates[path]
    if policy == "first":
        return options[0]
    if policy == "last":
        return options[-1]
    if policy == "random":
        return rng.choice(options)
    # The two shaping policies, both read off the schema's own gates (see `_gate_extremes`).
    extremes = s.closing if policy == "gate_closed" else s.opening
    return extremes.get(path, options[0])


_WALK_POLICIES = ("first", "last", "gate_closed", "gate_open", "random")
_WALK_STEP_CAP = 400


def _walk(s: Schema, policy: str, seed: int) -> list[Case]:
    """Answer one owed target per step, task by task, in SPOKEN order — a call's own shape."""
    rng = Random(seed)
    answers: dict[str, str] = {}
    cases: list[Case] = []
    for task in s.plan.tasks:
        for step in range(_WALK_STEP_CAP):
            owed = set(_pure_owed(s, task, answers))
            target = next(
                (
                    path
                    for question in owed_now(task, answers, s.shared)
                    for path in question.target_paths
                    if path in owed
                ),
                None,
            )
            if target is None:
                break
            cases.append(Case(f"walk:{policy}:{task.task_key}:{step}", dict(answers)))
            answers[target] = _pick(s, policy, target, rng)
        else:  # the cap would silently shorten the corpus; longest real walk is ~64 steps
            raise AssertionError(f"walk:{policy}:{task.task_key} hit the step cap")
    cases.append(Case(f"walk:{policy}:end", dict(answers)))
    return cases


_RANDOM_SEED = 20260813
_RANDOM_STATES = 120


def _random_states(s: Schema) -> list[Case]:
    """Random assignments over the gate-referenced leaves, plus a random slice of the rest.

    The second half matters as much as the first: a partly-owed fan-out only happens when some
    of its members are on file and others are not."""
    rng = Random(_RANDOM_SEED)
    gate_paths = sorted(s.gate_refs)
    other_paths = sorted(set(s.descriptors) - s.gate_refs)
    cases: list[Case] = []
    for i in range(_RANDOM_STATES):
        answers: dict[str, str] = {}
        for path in gate_paths:
            choice = rng.choice((None, *s.candidates[path]))
            if choice is not None:
                answers[path] = choice
        for path in rng.sample(other_paths, k=rng.randint(0, len(other_paths) // 2)):
            answers[path] = rng.choice(s.candidates[path])
        cases.append(Case(f"random:{_RANDOM_SEED}:{i:04d}", answers))
    return cases


def _all_answered(s: Schema) -> dict[str, str]:
    return {path: s.candidates[path][0] for path in s.descriptors}


def _covered(s: Schema, prefix: str, value: str) -> dict[str, str]:
    """`covered = value` for every `covered` leaf under `prefix`. The suffix is a convention
    production itself relies on (`question_plan._covered_gate`)."""
    return {
        path: value
        for path in s.descriptors
        if path.startswith(f"{prefix}.") and path.endswith(".covered")
    }


def _routing_branches(s: Schema) -> list[tuple[str, list[str]]]:
    """`(section key, branch paths)` for every routing `alternatives` — an either/or whose
    members are GROUPS. Derived the way `call_plan._exclusive_notes` derives it."""
    found: list[tuple[str, list[str]]] = []
    for section_key, section in s.doc.sections.items():
        for alternatives in section.alternatives or []:
            branches = [
                member
                for member in alternatives.members
                if isinstance(section.fields.get(member.split(".")[-1]), Group)
            ]
            if len(branches) >= 2:
                found.append((section_key, branches))
    return found


def _fanout_axes(s: Schema) -> list[tuple[str, list[str]]]:
    """`(task key, target paths)` for every question spanning three or more fields — the
    fan-outs whose partly-owed states `still_needed` exists for. Read off the compiled tree
    rather than a code list, so a ninth CPT code joining a panel is covered when it lands."""
    return [
        (task.task_key, question.target_paths)
        for task in s.plan.tasks
        for question in iter_questions(task.panels)
        if len(question.target_paths) >= 3
    ]


def _confirms_required_value(s: Schema) -> str | None:
    """The anchor value that makes its confirms required, found by EVALUATING the requiredness
    rule rather than restating a schema's "Family" as a test constant."""
    if s.confirm_task is None:
        return None
    anchor = next(iter(s.confirms_by_anchor))
    confirm = s.descriptors[sorted(s.confirm_paths)[0]]
    return next(
        (
            value
            for value in s.candidates[anchor]
            if is_required(confirm, {anchor: value}, s.shared)
        ),
        None,
    )


def _adversarial_states(s: Schema) -> list[Case]:
    """The states a random sweep is unlikely to hit, each one a shape that has bitten.

    Every shape comes from the document or the compiled tree, so a schema declaring no routing
    sets (or no confirms, or no fan-outs) simply contributes none of those states rather than
    contributing a hardcoded state that answers nothing."""
    gates_open = dict(s.opening)
    cases: list[Case] = [
        Case("adv:nothing-answered", {}),
        Case("adv:everything-answered", _all_answered(s)),
        Case("adv:every-gate-open", dict(gates_open)),
        Case("adv:every-gate-closed", dict(s.closing)),
    ]
    if (required_value := _confirms_required_value(s)) is not None:
        anchor = next(iter(s.confirms_by_anchor))
        task = s.confirm_task
        assert task is not None
        all_but_confirms = {
            **{
                field.path: s.candidates[field.path][0]
                for field in task.fields
                if field.path not in s.confirm_paths
            },
            anchor: required_value,
        }
        cases += [
            # The db8cc1c4 bug: only the confirms left, their anchor answered.
            Case("adv:confirms-only", dict(all_but_confirms)),
            Case("adv:confirm-anchor-nothing-else", {anchor: required_value}),
            Case(
                "adv:one-confirm-answered",
                {**all_but_confirms, sorted(s.confirm_paths)[0]: "Alex Doe"},
            ),
        ]
        # Every value the anchor can take, so the not-required side is covered too.
        for value in s.candidates[anchor]:
            cases.append(Case(f"adv:confirm-anchor-{value}", {anchor: value}))
    # Routing over groups: each branch taken alone, all refused, all inapplicable — and the
    # asymmetry that matters, one branch answered while the others are simply SILENT.
    for section_key, branches in _routing_branches(s):
        for taken in branches:
            leaf = taken.split(".")[-1]
            cases.append(
                Case(
                    f"adv:route-{section_key}-{leaf}",
                    {
                        **gates_open,
                        **_covered(s, taken, "Yes"),
                        **{
                            path: "N/A"
                            for other in branches
                            if other != taken
                            for path in _covered(s, other, "N/A")
                        },
                    },
                )
            )
            cases.append(
                Case(
                    f"adv:route-{section_key}-{leaf}-others-silent",
                    {**gates_open, **_covered(s, taken, "Yes")},
                )
            )
        for value in ("No", "N/A"):
            cases.append(
                Case(
                    f"adv:route-{section_key}-all-{value.replace('/', '')}",
                    {
                        **gates_open,
                        **{p: value for b in branches for p in _covered(s, b, value)},
                    },
                )
            )
    # Every fan-out axis at one, half and all members answered.
    for task_key, targets in _fanout_axes(s):
        for count in dict.fromkeys((1, max(1, len(targets) // 2), len(targets))):
            cases.append(
                Case(
                    f"adv:fanout-{task_key}-{targets[0].split('.')[-1]}-{count}-of-{len(targets)}",
                    {**gates_open, **{path: s.candidates[path][0] for path in targets[:count]}},
                )
            )
    # `not_in` gate boundaries on the money leaves, from the literals those gates name.
    money = [path for path, field in s.descriptors.items() if field.type == "currency"]
    boundaries = dict.fromkeys(value for path in money for value in s.gate_literals.get(path, []))
    for value in (*boundaries, "$500"):
        if money:
            cases.append(
                Case(
                    f"adv:money-{value.replace(' ', '-').replace('$', 'usd')}",
                    dict.fromkeys(money, value),
                )
            )
    # Every authored either/or, one side / the other / both.
    for group in s.plan.alternative_pairs:
        stem = group[0].rsplit(".", 1)[0].split(".")[-1]
        for label, filled in (
            (group[0].rsplit(".", 1)[-1], group[:1]),
            (group[-1].rsplit(".", 1)[-1], group[-1:]),
            ("both", group),
        ):
            cases.append(
                Case(
                    f"adv:either-or-{stem}-{label}",
                    {**gates_open, **{path: s.candidates[path][0] for path in filled}},
                )
            )
    # One gate-referenced field flipped at a time, against an otherwise-open plan: this is what
    # reaches a cross-task gate (scope pairs, prior auth, plan type) without naming one.
    for path in sorted(s.gate_refs):
        for value in s.candidates[path]:
            cases.append(
                Case(f"adv:gate-{path.split('.')[-1]}-{value}", {**gates_open, path: value})
            )
    return cases


@cache
def _states(s: Schema) -> tuple[Case, ...]:
    walks = [case for i, policy in enumerate(_WALK_POLICIES) for case in _walk(s, policy, seed=i)]
    return (*_adversarial_states(s), *walks, *_random_states(s))


# -- the sweep ----------------------------------------------------------------------------

Check = Callable[[Schema, PlanTask, Answers], str | None]


def _render_state(answers: Answers) -> str:
    return "\n".join(f"  {path} = {value!r}" for path, value in sorted(answers.items())) or "  {}"


def _shrink(s: Schema, check: Check, task: PlanTask, answers: Answers) -> dict[str, str]:
    """The smallest subset of `answers` that still fails `check` — one greedy pass."""
    current = dict(answers)
    for path in sorted(current):
        trial = {key: value for key, value in current.items() if key != path}
        if check(s, task, trial) is not None:
            current = trial
    return current


def _sweep(s: Schema, check: Check) -> int:
    """`check` over every (state, task); the count checked, and a minimal repro on failure."""
    checked = 0
    for case in _states(s):
        for task in s.plan.tasks:
            checked += 1
            problem = check(s, task, case.answers)
            if problem is None:
                continue
            minimal = _shrink(s, check, task, case.answers)
            pytest.fail(
                f"{s}: {case.label} / task {task.task_key}: "
                f"{check(s, task, minimal) or problem}\n"
                f"minimal reproducing answer state:\n{_render_state(minimal)}"
            )
    return checked


def _expected_slices(s: Schema) -> int:
    """Every (state, task) pair. Asserted by each sweep so an early return inside `_sweep`
    cannot leave an invariant vacuously green; non-vacuity of the CORPUS is a separate test,
    because a product of two cardinalities says nothing about what the states contain."""
    return len(_states(s)) * len(s.plan.tasks)


def test_the_corpus_reaches_every_shape_the_invariants_are_about(schema: Schema) -> None:
    """Non-vacuity, asserted on CONTENT. Every check below is universally quantified, so a
    corpus that quietly stopped building a shape would leave its invariant green while testing
    nothing.

    Each claim is conditional on the schema DECLARING that structure — which is what makes this
    meaningful for a new catalog: it demands coverage of whatever the new document contains,
    instead of demanding `ibv_standard`'s shapes from everyone."""
    s = schema
    states = _states(s)
    owed_somewhere = {
        task.task_key
        for case in states
        for task in s.plan.tasks
        if _pure_owed(s, task, case.answers)
    }
    assert owed_somewhere == set(s.task_index), "some task never owed anything in any state"

    values_seen: dict[str, set[str]] = {}
    for case in states:
        for path, value in case.answers.items():
            values_seen.setdefault(path, set()).add(value)
    thin = sorted(
        path
        for path in s.gate_refs
        if len(values_seen.get(path, set())) < min(2, len(s.candidates[path]))
    )
    assert thin == [], f"{len(thin)} gate-referenced path(s) never varied: {thin[:5]}"

    partly_owed = fully_owed = confirms_owed = routed = 0
    for case in states:
        for task in s.plan.tasks:
            owed, panels = _narrowed(s, task, case.answers)
            if s.confirm_paths.intersection(owed):
                confirms_owed += 1
            for question in iter_questions(panels):
                if question.routes_between:
                    routed += 1
                elif len(question.target_paths) > 1:
                    inside = sum(path in set(owed) for path in question.target_paths)
                    partly_owed += inside < len(question.target_paths)
                    fully_owed += inside == len(question.target_paths)
    if _fanout_axes(s) or s.plan.alternative_pairs:
        assert partly_owed and fully_owed, "no partly-owed fan-out was ever narrowed"
    if s.confirm_paths:
        assert confirms_owed, "no state ever owed a confirm node"
    if _routing_branches(s):
        assert routed, "no routing question ever survived a narrowing"


def _panels_of(tree: list[PromptPanel]) -> Iterator[PromptPanel]:
    for panel in tree:
        yield panel
        yield from _panels_of(panel.children)


_ORDINAL_RE = re.compile(r"^\s*(\d+)\. ", re.MULTILINE)


def _ordinals(text: str) -> list[int]:
    return [int(match.group(1)) for match in _ORDINAL_RE.finditer(text)]


def _targets(tree: list[PromptPanel]) -> set[str]:
    return {path for question in iter_questions(tree) for path in question.target_paths}


def _narrowed(s: Schema, task: PlanTask, answers: Answers) -> tuple[list[str], list[PromptPanel]]:
    """What this task owes, and its tree narrowed to exactly that — the pair almost every
    invariant opens with."""
    owed = _pure_owed(s, task, answers)
    return owed, focus_questions(task, owed, answers, s.shared)


def _digest(task: PlanTask, panels: list[PromptPanel]) -> str:
    """`task_sections` is the task's ORIGINAL root count, never the surviving one; stated once
    here so the callers cannot drift on it.

    Deliberately a narrowing of the COMPILED tree — the runtime's own refusal digest narrows the
    entry-gated tree instead (`_owed_digest`), which the guard tests assert through the guards
    themselves."""
    return render_digest(panels, task_sections=len(task.panels))


# -- invariant 1: no loss -----------------------------------------------------------------


def _check_no_loss(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    owed, panels = _narrowed(s, task, answers)
    listed = _targets(panels)
    if missing := [path for path in owed if path not in listed]:
        return f"{len(missing)} owed path(s) in no surviving question: {missing[:4]}"
    if not owed:
        return None
    if numbered_questions(panels) < 1:
        return f"{len(owed)} path(s) owed but the tree numbers no question"
    if not _digest(task, panels).strip():
        return f"{len(owed)} path(s) owed but the digest renders empty"
    if not render_panels(panels).strip():
        return f"{len(owed)} path(s) owed but the full render is empty"
    return None


def test_no_owed_question_is_ever_lost(schema: Schema) -> None:
    """Invariant 1, the one that matters most: a missed question is silent data loss."""
    assert _sweep(schema, _check_no_loss) == _expected_slices(schema)


def _check_the_worker_owes_what_the_rule_owes(
    s: Schema, task: PlanTask, answers: Answers
) -> str | None:
    """`gap_fields` is what the runtime actually re-asks, and it reaches the tree through
    `owed_now`. It may be narrower than the rule in exactly ONE documented way — an answered
    either/or sibling satisfies its partner. Anything else it drops is a required field no
    guard will report and no sweep will re-ask."""
    controller = _reading(s, answers)
    worker = {field.path for field in controller.gap_fields(s.task_index[task.task_key])}
    for path in _pure_owed(s, task, answers):
        if path in worker or any(
            has_value(answers, sibling) for sibling in s.alternatives.get(path, ())
        ):
            continue
        return f"{path} is owed but gap_fields does not report it"
    return None


def test_the_worker_owes_every_question_the_rule_owes(schema: Schema) -> None:
    """The join between the two halves of this file. Invariant 1 proves the TREE can render
    what is owed; this proves the runtime asks for it — a field missing here is invisible to
    both completion guards and to the gap sweep."""
    assert _sweep(schema, _check_the_worker_owes_what_the_rule_owes) == _expected_slices(schema)


def test_every_collectable_field_has_a_question_node(schema: Schema) -> None:
    """The precondition invariant 1 rests on — with a descriptor and no node, no narrowing
    could ever list the field. `owed_now` exempts an end-of-task confirm for exactly this
    reason; no catalog authors one, and this is where that would surface."""
    for task in schema.plan.tasks:
        orphans = sorted({field.path for field in task.fields} - _targets(task.panels))
        assert orphans == [], f"{task.task_key}: descriptors with no question node: {orphans}"


# -- invariant 2: no phantom --------------------------------------------------------------


def _check_no_phantom(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    owed_list, panels = _narrowed(s, task, answers)
    owed = set(owed_list)
    for question in iter_questions(panels):
        if question.routes_between:
            continue  # collects nothing; judged by invariant 4
        if not owed.intersection(question.target_paths):
            return f"question {question.text[:50]!r} survives owing nothing"
        if all(has_value(answers, path) for path in question.target_paths):
            return f"question {question.text[:50]!r} survives fully answered"
    return None


def test_no_answered_or_inapplicable_question_survives(schema: Schema) -> None:
    assert _sweep(schema, _check_no_phantom) == _expected_slices(schema)


# -- invariant 3: question atomicity + still_needed ----------------------------------------


def _check_still_needed(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    owed_list, panels = _narrowed(s, task, answers)
    owed = set(owed_list)
    for question in iter_questions(panels):
        targets = question.target_paths
        owed_here = [path for path in targets if path in owed]
        titles = [title for path in owed_here if (title := s.owner_title.get(path))]
        strict_subset = bool(owed_here) and len(owed_here) < len(targets)
        expected = (
            list(dict.fromkeys(titles)) if strict_subset and len(titles) == len(owed_here) else []
        )
        if question.still_needed != expected:
            return (
                f"question {question.text[:40]!r} owes {len(owed_here)}/{len(targets)} targets, "
                f"still_needed={question.still_needed} expected {expected}"
            )
        if unknown := [name for name in question.still_needed if name not in s.group_titles]:
            return f"still_needed names a non-group: {unknown}"
    return None


def test_a_partly_owed_fanout_names_exactly_the_members_it_needs(schema: Schema) -> None:
    assert _sweep(schema, _check_still_needed) == _expected_slices(schema)


def test_a_fanout_or_either_or_axis_is_exactly_one_question(schema: Schema) -> None:
    """Both axes collapse to ONE spoken question however many members they span — the property
    `gap_fields` being field-granular would otherwise re-ask member by member. Asserted over
    every authored axis, including `ibv_standard`'s 16-member diagnostic either/or that rides on
    two 8-code ask groups and must still come out as a single question."""
    s = schema
    section_to_task = s.doc.section_to_task()
    checked = 0
    for section_key, section in s.doc.sections.items():
        axes = [
            (f"{len(group.fields)}-member ask group", set(group.fields))
            for group in section.ask_groups or []
        ] + [
            (f"{len(members)}-member either/or", members)
            for alternatives in section.alternatives or []
            if (members := {m for m in alternatives.members if m in s.leaves})
        ]
        if not axes:
            continue
        task = next(t for t in s.plan.tasks if t.task_key == section_to_task[section_key])
        for label, members in axes:
            checked += 1
            owners = [
                question
                for question in iter_questions(task.panels)
                if members.intersection(question.target_paths)
            ]
            assert len(owners) == 1, f"{section_key}: {label} spans {len(owners)} questions"
            assert members <= set(owners[0].target_paths)
    if not checked:
        pytest.skip(f"{s} authors no ask group or either/or axis")


# -- invariant 4: routing questions --------------------------------------------------------


def _check_routing(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    _owed, panels = _narrowed(s, task, answers)
    for panel in _panels_of(panels):
        surviving = {child.title for child in panel.children}
        for question in panel.questions:
            if not question.routes_between:
                continue
            kept = surviving.intersection(question.routes_between)
            if len(kept) < 2:
                return (
                    f"routing question survives over {len(kept)} branch(es): "
                    f"{sorted(kept)} of {question.routes_between}"
                )
    if routed := [q for q in owed_now(task, answers, s.shared) if q.routes_between]:
        return f"{len(routed)} routing question(s) reported owed"
    return None


def test_a_routing_question_survives_only_with_two_branches(schema: Schema) -> None:
    assert _sweep(schema, _check_routing) == _expected_slices(schema)


# -- invariant 5: either/or sets -----------------------------------------------------------


def _check_either_or(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    controller = _reading(s, answers)
    owed = {field.path for field in controller.gap_fields(s.task_index[task.task_key])}
    for path in owed:
        answered = [
            sibling for sibling in s.alternatives.get(path, ()) if has_value(answers, sibling)
        ]
        if answered:
            return f"{path} still owed though its either/or sibling {answered[0]} is answered"
    return None


def test_answering_one_side_of_an_either_or_satisfies_the_set(schema: Schema) -> None:
    assert _sweep(schema, _check_either_or) == _expected_slices(schema)


# -- invariant 6: claimed counts match rendered lists --------------------------------------


def _check_counts(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    _owed, panels = _narrowed(s, task, answers)
    total = numbered_questions(panels)
    expected = list(range(1, total + 1))
    rendered = render_panels(panels)
    for name, text in (("digest", _digest(task, panels)), ("render", rendered)):
        if _ordinals(text) != expected:
            return f"{name} ordinals {_ordinals(text)[:6]} vs numbered_questions {total}"
    routed = {q.text for q in iter_questions(panels) if q.routes_between}
    confirms = {q.text for q in iter_questions(panels) if q.is_confirm}
    for line in rendered.splitlines():
        if (match := _ORDINAL_RE.match(line)) is None:
            continue
        body = line[match.end() :]
        if body in routed:
            return f"routing question took ordinal {match.group(1)}"
        if body in confirms:
            return f"nested confirm took ordinal {match.group(1)}"
    return None


def test_the_last_ordinal_equals_the_owed_ask_count(schema: Schema) -> None:
    assert _sweep(schema, _check_counts) == _expected_slices(schema)


# -- invariant 7: explode closure ----------------------------------------------------------


def _check_explode(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    owed = set(_pure_owed(s, task, answers))
    by_path = {field.path: field for field in task.fields}
    once = _exploded(task, owed, answers, s.shared, by_path)
    if _exploded(task, once, answers, s.shared, by_path) != once:
        return "the gate closure is not a fixpoint"
    if not owed <= once:
        return f"the closure dropped {len(owed - once)} owed path(s)"
    if answered := [path for path in once - owed if has_value(answers, path)]:
        return f"the closure pre-loaded {len(answered)} answered path(s): {answered[:3]}"
    plain = {
        tuple(q.target_paths)
        for q in iter_questions(focus_questions(task, owed, answers, s.shared))
    }
    wide_tree = focus_questions(task, owed, answers, s.shared, explode=True)
    wide = {tuple(q.target_paths) for q in iter_questions(wide_tree)}
    if not plain <= wide:
        return f"the exploded tree lost {len(plain - wide)} question(s) the owed tree had"
    if optional := [q.text[:40] for q in iter_questions(wide_tree) if q.optional]:
        return f"the exploded tree carries optional question(s): {optional[:3]}"
    return None


def test_the_exploded_set_is_a_closed_superset(schema: Schema) -> None:
    assert _sweep(schema, _check_explode) == _expected_slices(schema)


# -- invariant 8: keep/drop complementarity ------------------------------------------------


def _check_partition(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    """`keep_questions` and `drop_questions` partition the tree, with the two documented
    exemptions: a routing question survives under both rules, and `drop_questions` deliberately
    keeps a confirm travelling with its anchor (what keeps the compiled prompt byte-identical),
    so a confirm can be absent from both sides."""
    wanted = set(_pure_owed(s, task, answers))

    def ids(tree: list[PromptPanel]) -> set[tuple[str, ...]]:
        return {
            tuple(question.target_paths)
            for question in iter_questions(tree)
            if not question.routes_between
            and not s.confirm_paths.intersection(question.target_paths)
        }

    everything = ids(task.panels)
    kept = ids(keep_questions(task.panels, wanted))
    dropped = ids(drop_questions(task.panels, wanted))
    if kept | dropped != everything:
        lost = everything - (kept | dropped)
        return f"{len(lost)} question(s) in neither narrowing: {sorted(lost)[:2]}"
    overlap = {
        targets
        for targets in everything
        if wanted.intersection(targets) and not set(targets) <= wanted
    }
    if kept & dropped != overlap:
        unexpected = (kept & dropped) ^ overlap
        return f"{len(unexpected)} question(s) wrongly in both narrowings: {sorted(unexpected)[:2]}"
    return None


def test_keep_and_drop_partition_the_tree(schema: Schema) -> None:
    assert _sweep(schema, _check_partition) == _expected_slices(schema)


# -- invariant 9: the compiled tree is never touched ---------------------------------------


def test_narrowing_never_mutates_the_compiled_tree(schema: Schema) -> None:
    """Every narrowing is pure, and the compiler never sets `still_needed` — the two facts
    that keep `render_panels(task.panels)` byte-identical to the shipped prompt."""
    s = schema
    before = {task.task_key: render_panels(task.panels) for task in s.plan.tasks}
    assert not [
        question
        for task in s.plan.tasks
        for question in iter_questions(task.panels)
        if question.still_needed
    ]
    for case in _states(s):
        for task in s.plan.tasks:
            owed = _pure_owed(s, task, case.answers)
            focus_questions(task, owed, case.answers, s.shared, explode=True)
            keep_questions(task.panels, owed)
            drop_questions(task.panels, set(owed))
    assert {task.task_key: render_panels(task.panels) for task in s.plan.tasks} == before


def test_the_rendered_tree_is_the_shipped_prompt(schema: Schema) -> None:
    """The plan this file simulates over is the one the worker loads, not a lookalike."""
    for task in schema.plan.tasks:
        if task.panels:
            assert render_panels(task.panels) in task.prompt


# -- invariant 10: confirm nodes -----------------------------------------------------------


def _check_owed_confirm(s: Schema, task: PlanTask, answers: Answers) -> str | None:
    owed, panels = _narrowed(s, task, answers)
    confirms = [path for path in owed if path in s.confirm_paths]
    if not confirms:
        return None
    digest = _digest(task, panels)
    by_target = {
        path: question for question in iter_questions(panels) for path in question.target_paths
    }
    for path in confirms:
        question = by_target.get(path)
        if question is None:
            return f"owed confirm {path} is in no surviving question"
        if question.text not in digest:
            return f"owed confirm {path} is not named in the digest"
        # Per confirm, not per plan: a second anchored confirm elsewhere must not decide this
        # one. Unreachable on `ibv_standard` — a confirm is required only once its own anchor is
        # answered, so an owed confirm's anchor never is — the nested case is covered below.
        anchored = s.anchor_of[path] in owed
        if anchored and not question.is_confirm:
            return f"confirm {path} kept beside its anchor lost is_confirm"
        if not anchored and question.is_confirm:
            return f"orphaned confirm {path} was not promoted, so it renders as nothing"
    return None


def test_an_owed_confirm_is_always_asked(schema: Schema) -> None:
    """The db8cc1c4 regression: with the anchor answered and only the confirms outstanding, the
    refusal listed nothing and the gap pass zeroed its own turn ceiling."""
    assert _sweep(schema, _check_owed_confirm) == _expected_slices(schema)


def test_the_confirm_bug_state_is_actually_simulated(schema: Schema) -> None:
    """A green invariant proves nothing if no state reaches the shape it guards."""
    s = schema
    if s.confirm_task is None:
        pytest.skip(f"{s} authors no confirm_immediate leaf")
    case = next(c for c in _states(s) if c.label == "adv:confirms-only")
    owed = _pure_owed(s, s.confirm_task, case.answers)
    assert set(owed) == s.confirm_paths
    panels = focus_questions(s.confirm_task, owed, case.answers, s.shared)
    assert numbered_questions(panels) == len(s.confirm_paths)
    assert _digest(s.confirm_task, panels).strip()


def test_a_confirm_kept_beside_its_anchor_stays_an_unnumbered_bullet(schema: Schema) -> None:
    """The other half of invariant 10, and the half that keeps the compiled prompt
    byte-identical: promotion is for an ORPHANED confirm only. The owed set can never produce
    this pairing (see `_check_owed_confirm`), so it is asserted against a narrowing that
    deliberately wants the anchor and its confirms together."""
    s = schema
    if s.confirm_task is None:
        pytest.skip(f"{s} authors no confirm_immediate leaf")
    task = s.confirm_task
    for anchor, pairs in s.confirms_by_anchor.items():
        anchored = [path for path, _line in pairs]
        anchor_question = next(
            question for question in iter_questions(task.panels) if anchor in question.target_paths
        )

        together = focus_questions(task, {anchor, *anchored}, {}, s.shared)
        kept = list(iter_questions(together))
        assert [question.is_confirm for question in kept] == [False, *[True] * len(anchored)]
        assert numbered_questions(together) == 1  # the anchor's ordinal; the confirms take none
        rendered = render_panels(together)
        assert _ordinals(rendered) == [1]
        assert f"1. {anchor_question.text}" in rendered
        for question in kept[1:]:
            # Nested under the anchor, not a line of its own.
            assert f"     * {question.text}" in rendered

        alone = focus_questions(task, set(anchored), {}, s.shared)
        assert [q.is_confirm for q in iter_questions(alone)] == [False] * len(anchored)
        assert _ordinals(render_panels(alone)) == list(range(1, len(anchored) + 1))


# -- the worker's guard arithmetic ---------------------------------------------------------


def _build_controller(s: Schema, answers: Answers) -> PlanRunController:
    """A FRESH controller. Needed wherever agents are entered — `on_enter` rewrites
    instructions and snapshots a ceiling, so agent state must not carry between states."""
    controller = PlanRunController(
        s.plan,
        room_name="call--t--c",
        run_state=FakeRunState(),  # type: ignore[arg-type]
    )
    controller.update_answers(dict(answers))
    return controller


@cache
def _reader(s: Schema) -> PlanRunController:
    """One controller per schema, re-pointed at each state, for the sweeps that only READ it.

    `update_answers` replaces the map wholesale over the (empty) baseline and `gap_fields` is a
    pure function of it, so no state leaks into the next — and building an agent per task per
    state to answer one question about the answer map costs most of the file's runtime."""
    return _build_controller(s, {})


def _reading(s: Schema, answers: Answers) -> PlanRunController:
    controller = _reader(s)
    controller.update_answers(dict(answers))
    return controller


async def _enter(agent: Agent, controller: PlanRunController) -> MagicMock:
    session = MagicMock()
    with _session_patch(agent, session):
        await agent.on_enter()
        await controller.drain_cursor_writes()
    return session


@cache
def _guard_states(s: Schema) -> tuple[Case, ...]:
    """Every adversarial state plus a thinned slice of the generated ones: a guard check builds
    real agents and rewrites instruction strings, so it is orders of magnitude dearer than a
    tree narrowing. A stride, not a prefix, so the sample spans the whole corpus."""
    states = _states(s)
    adversarial = [case for case in states if case.label.startswith("adv:")]
    generated = [case for case in states if not case.label.startswith("adv:")]
    return (*adversarial[::3], *generated[::37])


async def test_the_turn_ceiling_matches_the_list_the_agent_is_given(schema: Schema) -> None:
    """`_questions_at_entry` is the bound both completion guards judge by; measured against a
    different list than the agent reads, it releases the guard with questions still unasked."""
    s = schema
    checked = skipped = 0
    for case in _guard_states(s):
        controller = _build_controller(s, case.answers)
        for index, task in enumerate(controller.plan.tasks):
            agent = controller.agents[index]
            session = await _enter(agent, controller)
            if session.update_agent.called:
                skipped += 1  # every question gated out: the task handed straight on
                continue
            checked += 1
            ceiling = agent._questions_at_entry
            listed = _ordinals(agent.instructions)
            assert listed == list(range(1, ceiling + 1)), (
                f"{s}: {case.label} / {task.task_key}: ceiling {ceiling} vs "
                f"{len(listed)} numbered question(s) in the instructions"
            )
            stated = _stated_total(agent.instructions)
            assert stated == ceiling, (
                f"{s}: {case.label} / {task.task_key}: COMPLETENESS claims {stated}, "
                f"ceiling {ceiling}"
            )
    # Exact, not a floor: every (state, task) pair is either entered or handed straight on.
    assert checked and checked + skipped == len(_guard_states(s)) * len(s.plan.tasks)


async def test_a_task_refusal_always_names_at_least_one_question(schema: Schema) -> None:
    """The failure shape of the confirm bug: a refusal whose rendered list was empty, which
    also zeroed the count the message claimed."""
    s = schema
    checked = eligible = 0
    for case in _guard_states(s):
        controller = _build_controller(s, case.answers)
        for index, task in enumerate(controller.plan.tasks):
            if not controller.gap_fields(index):
                continue
            eligible += 1
            agent = controller.agents[index]
            session = await _enter(agent, controller)
            if session.update_agent.called:
                continue
            assert agent._questions_at_entry >= 1, (
                f"{s}: {case.label} / {task.task_key}: "
                f"{len(controller.gap_fields(index))} field(s) owed but the turn ceiling is 0, "
                "so the guard can never refuse"
            )
            # Through the TOOL, where a refusal is what it returns — so this covers the drain
            # gate and the double-advance interlock on the path the model actually takes.
            with _session_patch(agent, MagicMock()):
                refusal = await _completion_tool(agent)()
            checked += 1
            assert isinstance(refusal, str), (
                f"{s}: {case.label} / {task.task_key}: advanced with "
                f"{len(controller.gap_fields(index))} field(s) owed at turn 0"
            )
            assert _ordinals(refusal), f"{s}: {case.label} / {task.task_key}: lists nothing"
    assert checked and eligible


async def test_a_gap_sweep_lists_and_counts_the_same_questions(schema: Schema) -> None:
    """The gap block claims a required count, renders an exploded list, and takes its turn
    ceiling off the list — three numbers that have disagreed before."""
    s = schema
    checked = 0
    for case in _guard_states(s):
        controller = _build_controller(s, case.answers)
        for agent in controller.gap_agents:
            index = agent.task_index
            fields = controller.gap_fields(index)
            if not fields:
                continue
            checked += 1
            key = controller.plan.tasks[index].task_key
            required = numbered_questions(controller.gap_panels(index, fields))
            block, listed = agent._gap_text(fields)
            ordinals = _ordinals(block)
            assert listed == len(ordinals) == max(ordinals), (
                f"{s}: {case.label} / {key}: block lists {len(ordinals)} question(s), "
                f"ceiling says {listed}"
            )
            assert 1 <= required <= listed
            # The plural renders "N required questions are", so the singular is a prefix of both.
            assert f"{required} required question" in block
            session = await _enter(agent, controller)
            if session.update_agent.called:
                continue
            assert agent._questions_owed == listed
            with _session_patch(agent, MagicMock()):
                refusal = await _completion_tool(agent)()
            assert isinstance(refusal, str), f"{s}: {case.label} / {key}: swept away owed fields"
            match = re.search(r"recorded yet for (\d+) of the follow-up", refusal)
            assert match is not None, f"{s}: {case.label} / {key}: count unparseable"
            ordinals = _ordinals(refusal)
            assert int(match.group(1)) == len(ordinals) >= 1, (
                f"{s}: {case.label} / {key}: refusal claims {match.group(1)} question(s) over a "
                f"list of {len(ordinals)}"
            )
    assert checked


async def test_the_drain_is_paid_exactly_when_a_refusal_is_reachable(schema: Schema) -> None:
    """`settle_before_refusing` buys the guard a fresher answer snapshot. It must be paid
    whenever a refusal can still be returned, and never when it cannot — the wait lands
    mid-conversation with the representative listening."""
    s = schema
    controller = _reader(s)
    observer = FakeObserverManager()
    controller.attach_observer(observer)  # type: ignore[arg-type]
    for case in _guard_states(s):
        controller.update_answers(dict(case.answers))
        for index in range(len(controller.plan.tasks)):
            owed = bool(controller.gap_fields(index))
            before = observer.drains
            await controller.settle_before_refusing(index)
            assert (observer.drains > before) is owed, (
                f"{s}: {case.label} / task {index}: drained={observer.drains > before} owed={owed}"
            )
    # Every drain it did pay used the guard's own tighter bound, not the Observer's default.
    assert observer.timeouts and set(observer.timeouts) == {_REFUSAL_DRAIN_TIMEOUT_S}


async def test_past_the_turn_ceiling_a_completion_neither_drains_nor_refuses(
    schema: Schema,
) -> None:
    """The other half of the drain gate: once the ceiling is spent the guard can no longer
    refuse, so paying the drain there is up to 4s of silence bought for nothing.

    Driven through the TOOL and through real rep turns, because the gate under test is in
    `task_complete` (`if self._rep_turns < self._questions_at_entry`), not in the guard — the
    guard is sync and could never drain, so asserting on it proved nothing."""
    s = schema
    controller = _build_controller(s, {})
    observer = FakeObserverManager()
    controller.attach_observer(observer)  # type: ignore[arg-type]
    index, agent = next(
        (index, controller.agents[index])
        for index in range(len(controller.plan.tasks))
        if controller.gap_fields(index)
    )
    await _enter(agent, controller)
    assert agent._questions_at_entry >= 1

    with _session_patch(agent, MagicMock()):
        # One turn short of the ceiling: a refusal is still reachable, so the drain is paid.
        for _ in range(agent._questions_at_entry - 1):
            await _rep_turn(agent, "Mm.")
        assert isinstance(await _completion_tool(agent)(), str)
        assert observer.drains == 1
        # The turn that spends the ceiling: it advances, in silence.
        await _rep_turn(agent, "Mm.")
        assert not isinstance(await _completion_tool(agent)(), str)
        assert observer.drains == 1
    assert controller.gap_fields(index), "the state under test must still owe something"


async def test_the_refusal_budget_always_lets_the_call_move_on(schema: Schema) -> None:
    """A rep who cannot answer never empties the owed set; the budget is what stops the plan
    stranding on the task. Asserted on the refusal COUNT, never on the model's behaviour."""
    s = schema
    for case in _guard_states(s):
        controller = _build_controller(s, case.answers)
        for index in range(len(controller.plan.tasks)):
            if not controller.gap_fields(index):
                continue
            agent = controller.agents[index]
            session = await _enter(agent, controller)
            if session.update_agent.called:
                continue
            refusals = 0
            with _session_patch(agent, session):
                complete = _completion_tool(agent)
                for _ in range(_TASK_FRUITLESS_REFUSALS + 2):
                    if not isinstance(await complete(), str):
                        break
                    refusals += 1
                    # A rep turn between calls, or `_advanced_this_turn` answers instead of the
                    # guard — and that string is not a refusal.
                    await _rep_turn(agent, "Mm.")
            assert refusals <= _TASK_FRUITLESS_REFUSALS, (
                f"{s}: {case.label} / task {index}: {refusals} fruitless refusals"
            )


# -- whole-call drives ---------------------------------------------------------------------
#
# The per-state checks above judge one snapshot. These compose them: a scripted representative
# answers a whole question per turn — the unit the turn ceiling is justified by ("N questions
# cannot be asked in fewer than N exchanges") — and the real chain runs to wrap-up, guards,
# budgets, gap pass and all. What this catches that a snapshot cannot is a question the call
# NEVER put in front of the agent, which the gap pass running once makes unreachable.

_CALL_STEP_CAP = 600


@dataclass
class CallRun:
    answers: dict[str, str]
    spoken: set[str] = dataclass_field(default_factory=set)
    # Every question a task owed WHILE its agent held the floor. Accumulated per task rather
    # than plan-wide because a question owed in a later task legitimately goes unspoken until
    # the call gets there — and may be gated out before it does.
    owed_in_task: set[str] = dataclass_field(default_factory=set)
    # Gap-sweep lines still naming a question with nothing left to collect.
    stale_listed: set[str] = dataclass_field(default_factory=set)
    swept: set[str] = dataclass_field(default_factory=set)  # task keys the gap pass reached
    visited: set[str] = dataclass_field(default_factory=set)  # task keys the MAIN pass entered
    # Sweepable tasks still owing something when the gap pass began (see `_sweep_candidates`).
    expected_sweep: set[str] = dataclass_field(default_factory=set)
    refusals: int = 0
    steps: int = 0
    reached_wrap_up: bool = False


def _listed_lines(text: str) -> set[str]:
    """The question sentences a block puts in front of the agent — its numbered lines and the
    nested confirm bullets under them."""
    lines: set[str] = set()
    for line in text.splitlines():
        if (match := _ORDINAL_RE.match(line)) is not None:
            lines.add(line[match.end() :])
        elif line.startswith("     * "):
            lines.add(line.split("* ", 1)[1])
    return lines


def _fully_answered_lines(s: Schema, instructions: str, answers: Answers) -> set[str]:
    """Listed questions with nothing left to collect.

    Only ever asserted against a GAP block: a main-pass task lists everything it asks and tells
    the agent to confirm what is already on file, while the sweep's list is owed-only (plus the
    gate-closure follow-ups, which by construction have no value on any target). A sweep that
    keeps naming an answered question re-asks what the representative just said."""
    stale: set[str] = set()
    for line in _listed_lines(instructions):
        groups = s.text_targets.get(line)
        if groups and all(has_value(answers, path) for group in groups for path in group):
            stale.add(line)
    return stale


def _owed_texts(s: Schema, controller: PlanRunController, index: int) -> set[str]:
    """The spoken question behind every field this task still owes."""
    return {s.question_text[field.path] for field in controller.gap_fields(index)}


def _plan_owed_texts(s: Schema, controller: PlanRunController) -> set[str]:
    return {
        text
        for index in range(len(controller.plan.tasks))
        for text in _owed_texts(s, controller, index)
    }


def _completion_tool(agent: Agent) -> Callable[[], Awaitable[Any]]:
    """This agent's chain-advancing tool, `reason` pre-bound. A refusal is what it RETURNS —
    a `str` instead of the next agent — which is why every guard assertion above goes through
    the tool rather than the private guard it calls."""
    return _tool(agent, "gap_complete" if isinstance(agent, GapTaskAgent) else "task_complete")


def _sweep_candidates(s: Schema, controller: PlanRunController, visited: set[str]) -> set[str]:
    """Task keys the gap pass owes a sweep, as of NOW: below the closing task (which is never
    swept, so its reference number and goodbye stay last), entered by the main pass, still
    owing something. Snapshotted when the sweep begins — an answer landing mid-sweep can
    legitimately close a later task's gate."""
    return {
        task.task_key
        for index, task in enumerate(s.plan.tasks[:-1])
        if task.task_key in visited and controller.gap_fields(index)
    }


def _unswept_gaps(s: Schema, run: CallRun) -> set[str]:
    """Tasks that owed something when the sweep began, were never swept, and owe something
    still — answers the call can no longer reach, because the gap pass runs once."""
    controller = _reading(s, run.answers)
    return {
        key for key in run.expected_sweep - run.swept if controller.gap_fields(s.task_index[key])
    }


def _owed_question_targets(s: Schema, controller: PlanRunController, index: int) -> list[str]:
    """The target paths of the FIRST question this task still owes — what a representative
    answers in one breath ("are these eight codes covered?" is one answer, not eight)."""
    owed = {field.path for field in controller.gap_fields(index)}
    return _first_owed(s, controller.plan.tasks[index], controller.answers, owed)


def _first_owed(s: Schema, task: PlanTask, answers: Answers, owed: set[str]) -> list[str]:
    """The owed targets of the first question `task` owes, in SPOKEN order.

    One question is one breath — the unit the turn ceiling is justified by — so both state
    generators have to agree on it, which is why neither open-codes this."""
    for question in owed_now(task, answers, s.shared):
        if hit := [path for path in question.target_paths if path in owed]:
            return hit
    return []


async def _drive_call(s: Schema, policy: str, seed: int, *, gap_only: bool = False) -> CallRun:
    """Run the real agent chain to wrap-up with a scripted representative.

    `gap_only` keeps the representative silent through the main pass, so every task's questions
    survive into the gap sweep: that is the only shape where a sweep holds SEVERAL owed
    questions while the rep answers them one at a time, which is what exercises the
    re-narrow — and the sweep runs once, so a list that goes stale there is unrecoverable."""
    controller = _build_controller(s, {})
    controller.attach_observer(FakeObserverManager())  # type: ignore[arg-type]
    rng = Random(seed)
    answers: dict[str, str] = {}
    run = CallRun(answers)
    agent: Agent = controller.first_agent()
    entered: set[int] = set()
    while run.steps < _CALL_STEP_CAP:
        run.steps += 1
        if agent is controller.wrap_up_agent:
            run.reached_wrap_up = True
            break
        index = agent._task_index  # type: ignore[attr-defined]
        if id(agent) not in entered:
            entered.add(id(agent))
            key = controller.plan.tasks[index].task_key
            if isinstance(agent, GapTaskAgent):
                if not run.expected_sweep:
                    run.expected_sweep = _sweep_candidates(s, controller, run.visited)
                run.swept.add(key)
            session = await _enter(agent, controller)
            if session.update_agent.called:  # gated out, or nothing left to sweep
                run.swept.discard(key)
                agent = session.update_agent.call_args[0][0]
                continue
            if not isinstance(agent, GapTaskAgent):
                run.visited.add(key)
        run.spoken |= _listed_lines(agent.instructions)
        run.owed_in_task |= _owed_texts(s, controller, index)
        with _session_patch(agent, MagicMock()):
            if not gap_only or isinstance(agent, GapTaskAgent):
                for path in _owed_question_targets(s, controller, index):
                    answers[path] = _pick(s, policy, path, rng)
                controller.update_answers(answers)
            await _rep_turn(agent, "Sure.")
            run.spoken |= _listed_lines(agent.instructions)
            # Only a GAP agent's list is owed-only, and only while the sweep still owes
            # something: `on_user_turn_completed` deliberately skips the re-narrow when the set
            # EMPTIES, so the turn the last answer lands on keeps a list naming it — and the
            # agent's next move is gap_complete, which the guard passes. Asserting through that
            # would be asserting against the design.
            if isinstance(agent, GapTaskAgent) and controller.gap_fields(index):
                run.stale_listed |= _fully_answered_lines(s, agent.instructions, answers)
            outcome = await _completion_tool(agent)()
        if isinstance(outcome, str):
            run.refusals += 1
            # A refusal re-lists what it wants asked. Credit only — `render_digest` decorates
            # each line (`[either: …]`, `(only if …)`), so a digest line is not the question's
            # own sentence and cannot be resolved back to its targets; the instruction blocks
            # above are what `render_panels` emits verbatim, and they carry the stale check.
            run.spoken |= _listed_lines(outcome)
            continue
        agent = outcome
    return run


@pytest.mark.parametrize("policy", _WALK_POLICIES)
async def test_a_whole_call_asks_every_question_it_owes(schema: Schema, policy: str) -> None:
    """The composed property: drive the real chain to wrap-up, then assert every question a
    task owed while it held the floor was on the list its agent was holding, and that nothing
    required ends the call both unanswered and never asked."""
    s = schema
    # Never `hash(policy)`: str hashing is salted per process, so the `random` policy would
    # drive a different call on every run and a red run could not be replayed.
    run = await _drive_call(s, policy, seed=_WALK_POLICIES.index(policy))
    assert run.reached_wrap_up, f"{s}: {policy} never reached wrap-up in {run.steps} steps"
    left_owed = _plan_owed_texts(s, _reading(s, run.answers))
    # A drive that owes nothing anywhere would make the assertion below vacuous.
    assert run.owed_in_task, f"{s}: {policy} owed nothing at any point"
    unasked = sorted((run.owed_in_task | left_owed) - run.spoken)
    assert unasked == [], (
        f"{s}: {policy}: {len(unasked)} required question(s) were owed but never put to the "
        "agent:\n" + "\n".join(f"  {text}" for text in unasked[:8])
    )
    assert sorted(run.stale_listed) == [], (
        f"{s}: {policy}: {len(run.stale_listed)} owed-only list(s) still named a question with "
        "nothing left to collect:\n"
        + "\n".join(f"  {text}" for text in sorted(run.stale_listed)[:8])
    )


@pytest.mark.parametrize("policy", ("first", "gate_open"))
async def test_a_gap_sweep_stops_naming_a_question_once_it_is_answered(
    schema: Schema, policy: str
) -> None:
    """A representative who says nothing until the sweep leaves every task's questions to it, so
    each sweep holds a list of several and re-narrows it answer by answer. A sweep that keeps
    naming what was just answered re-asks the representative what they have already said."""
    s = schema
    run = await _drive_call(s, policy, seed=7, gap_only=True)
    assert run.reached_wrap_up
    # With nothing answered before the sweep, every sweepable task the call VISITED still owes
    # something, so the pass must reach all of them — a floor here would pass a sweep that
    # silently stopped after the first task, which is the shape that strands answers for good.
    assert run.expected_sweep, "the main pass left nothing for the sweep"
    assert _unswept_gaps(s, run) == set(), (
        f"{s}: the gap pass never reached {sorted(_unswept_gaps(s, run))}, which still owe"
    )
    assert len(run.owed_in_task) >= len(run.expected_sweep)
    assert sorted(run.stale_listed) == [], (
        f"{s}: {policy}: {len(run.stale_listed)} sweep list(s) still named an answered "
        "question:\n" + "\n".join(f"  {text}" for text in sorted(run.stale_listed)[:8])
    )
    unasked = sorted(run.owed_in_task - run.spoken)
    assert unasked == [], f"{s}: {policy}: {len(unasked)} owed question(s) never asked"


async def test_a_representative_who_answers_nothing_still_hears_every_question(
    schema: Schema,
) -> None:
    """The other extreme, and the one both guards exist for: the bot tries to finish every task
    the moment it enters — before any rep turn — and the representative never answers. Every
    refusal budget is spent, so the call still walks, and the gap pass (which runs ONCE) is the
    last chance for everything the main pass skipped.

    A separate loop from `_drive_call` on purpose: the ORDER is the thing under test here. This
    one completes before the first rep turn and insists to exhaustion inside one step; the drive
    answers first and completes after. Collapsing them behind a flag would hide that."""
    s = schema
    controller = _build_controller(s, {})
    controller.attach_observer(FakeObserverManager())  # type: ignore[arg-type]
    run = CallRun({})
    agent: Agent = controller.first_agent()
    for _ in range(_CALL_STEP_CAP):
        if agent is controller.wrap_up_agent:
            run.reached_wrap_up = True
            break
        index = agent._task_index  # type: ignore[attr-defined]
        session = await _enter(agent, controller)
        if session.update_agent.called:
            agent = session.update_agent.call_args[0][0]
            continue
        key = controller.plan.tasks[index].task_key
        if isinstance(agent, GapTaskAgent):
            if not run.expected_sweep:
                run.expected_sweep = _sweep_candidates(s, controller, run.visited)
            run.swept.add(key)
        else:
            run.visited.add(key)
        run.spoken |= _listed_lines(agent.instructions)
        run.owed_in_task |= _owed_texts(s, controller, index)
        with _session_patch(agent, MagicMock()):
            complete = _completion_tool(agent)
            while True:
                outcome = await complete()
                if not isinstance(outcome, str):
                    break
                run.refusals += 1
                run.spoken |= _listed_lines(outcome)
                await _rep_turn(agent, "Mm.")
                run.spoken |= _listed_lines(agent.instructions)
        agent = outcome
    assert run.reached_wrap_up, "the chain never reached wrap-up"
    # Non-empty first: with no gap pass at all both sets are empty and the equality below holds
    # vacuously, which is exactly the regression it is here to catch.
    assert run.expected_sweep, "the gap pass never ran, so nothing was ever re-asked"
    assert run.expected_sweep == run.swept, (
        f"{s}: the gap pass reached {sorted(run.swept)}, not the {sorted(run.expected_sweep)} owing"
    )
    # Both budgets are spent at least once each; the exact total is the models' to vary.
    assert run.refusals >= _TASK_FRUITLESS_REFUSALS + _GAP_FRUITLESS_REFUSALS
    still_owed = _plan_owed_texts(s, controller)
    assert still_owed, "nothing was answered, so the check below must have work to do"
    never_asked = sorted((run.owed_in_task | still_owed) - run.spoken)
    assert never_asked == [], (
        f"{s}: {len(never_asked)} required question(s) were never put to the agent:\n"
        + "\n".join(f"  {text}" for text in never_asked[:8])
    )


def test_a_focused_retry_narrowing_never_raises_or_names_a_ghost(schema: Schema) -> None:
    """`focus_call_plan` narrows descriptors but not panels (a known, deferred seam), so the
    only promises here are that nothing explodes and `still_needed` never names a member the
    narrowed plan has no descriptor for."""
    s = schema
    for keep_every in (2, 3):
        focused = focus_call_plan(s.plan, sorted(s.descriptors)[::keep_every])
        for task in focused.tasks:
            titles = {f.owner_title for f in task.fields if f.owner_title is not None}
            for case in _guard_states(s):
                owed = _pure_owed(s, task, case.answers)
                for question in iter_questions(
                    focus_questions(task, owed, case.answers, s.shared, explode=True)
                ):
                    ghosts = [name for name in question.still_needed if name not in titles]
                    assert not ghosts, f"{s}: {case.label}: still_needed names {ghosts}"
