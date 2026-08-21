"""Whole-form retry-scope simulation: the four global invariants a focused retry's ask set
must satisfy — proven by mutating `focus_paths` itself, never by re-deriving expectations from
it (five tests on this branch already shipped passing with their feature removed).

Expectations come only from `vera_core.forms.conditions` primitives (`leaf_gates`,
`is_applicable`, `is_required`) and `FormSchemaDoc`'s own structural accessors — never from
`focus_paths`, `_required_paths`, `expand_to_groups`, `_confirmed`, `is_call_confirmed`, or
`satisfied_required_fraction`. See docs/superpowers/plans/2026-08-21-f-retry-scope-simulation-
suite.md and docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from vera_core.forms.conditions import is_applicable, is_required, is_v2, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, Condition, FormSchemaDoc
from vera_core.forms.review import FieldStatus, focus_paths

AUTH, OTHER = uuid4(), uuid4()

_FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
_RAW: dict[str, Any] = json.loads((_FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text())
_DOC = FormSchemaDoc.model_validate(_RAW)

Triple = tuple[dict[str, FieldStatus], dict[str, Any], frozenset[UUID]]


@dataclass(frozen=True)
class Scenario:
    """One form's state going into a retry: what is on file, and whether an AUTHORITATIVE
    call collected it."""

    name: str
    values: dict[str, Any]
    confirmed_by_authoritative: bool

    def to_triple(self) -> Triple:
        """The `(status_by_path, values, authoritative_calls)` triple `focus_paths` needs."""
        call_id = AUTH if self.confirmed_by_authoritative else OTHER
        status = {path: FieldStatus("ai_call", True, 95, call_id) for path in self.values}
        return status, dict(self.values), frozenset({AUTH})

    def to_intake_triple(self) -> Triple:
        """Same values, sourced from intake — never confirmed by a call (spec D8)."""
        status = {path: FieldStatus("intake", None, None, None) for path in self.values}
        return status, dict(self.values), frozenset({AUTH})


def _focus(scenario: Scenario, *, intake: bool = False) -> list[str]:
    status, values, calls = scenario.to_intake_triple() if intake else scenario.to_triple()
    return focus_paths(_DOC, status, _RAW, floor=70, values=values, authoritative_calls=calls)


def _owed_now(doc: FormSchemaDoc, scenario: Scenario) -> set[str]:
    """Required + applicable + collectable, and not confirmed by an authoritative call, under
    `scenario.values` — built only from `leaf_gates`/`is_applicable`/`is_required`, so it is
    independent of `focus_paths`. `default` never enters `is_required`/`is_applicable`, so a
    defaulted leaf is included here exactly as the retry rule (`include_defaulted=True`) needs."""
    shared = doc.shared_conditions or {}
    confirmed = set(scenario.values) if scenario.confirmed_by_authoritative else set()
    return {
        path
        for path, leaf, gates in leaf_gates(doc)
        if leaf.role in COLLECTED_ROLES
        and is_applicable(gates, scenario.values, shared)
        and is_required(leaf, scenario.values, shared)
        and path not in confirmed
    }


def _innermost_group(doc: FormSchemaDoc, path: str) -> str | None:
    """The most specific group containing `path`, or `None` for a section-level leaf."""
    best: str | None = None
    for group in doc.group_paths():
        if path.startswith(f"{group}.") and (best is None or len(group) > len(best)):
            best = group
    return best


def _assert_soundness(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I1 — never ask a question that must not be asked."""
    shared = doc.shared_conditions or {}
    gated = list(leaf_gates(doc))
    gates_by_path: dict[str, tuple[Condition, ...]] = {path: gates for path, _leaf, gates in gated}
    roles_by_path = {path: leaf.role for path, leaf, _gates in gated}
    call_scoped = doc.collected_per_call_paths()
    owed = _owed_now(doc, scenario)
    # `expand_to_groups` pulls a group in on ANY owed member without re-checking each member's
    # own gate, so an unconfirmed sibling can legitimately drag in a still-inapplicable one.
    owed_groups = {g for path in owed for g in (_innermost_group(doc, path),) if g is not None}
    for path in focus:
        if path in call_scoped:
            continue
        assert path in roles_by_path, f"focus path is not a leaf at all: {path}"
        assert roles_by_path[path] in COLLECTED_ROLES, f"non-collectable path in focus: {path}"
        if is_applicable(gates_by_path[path], scenario.values, shared):
            continue
        group = _innermost_group(doc, path)
        assert group in owed_groups, (
            f"unsound: {path} has an unsatisfied gate and no owed sibling explains its presence"
        )


def _assert_completeness(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I2 — never skip a question that must be asked, including a defaulted leaf."""
    missing = _owed_now(doc, scenario) - set(focus)
    assert not missing, f"incomplete: {len(missing)} owed path(s) missing from focus"


def _assert_group_closure(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I3 — a partly-owed group is asked whole."""
    shared = doc.shared_conditions or {}
    gates_by_path = {path: gates for path, _leaf, gates in leaf_gates(doc)}
    focus_set = set(focus)
    collectable = doc.collection_paths()
    for group in doc.group_paths():
        prefix = f"{group}."
        members = [path for path in collectable if path.startswith(prefix)]
        if not members or not any(member in focus_set for member in members):
            continue
        for member in members:
            if is_applicable(gates_by_path[member], scenario.values, shared):
                assert member in focus_set, f"group closure gap: {member} missing from {group}"


def _assert_call_scoped_always(doc: FormSchemaDoc, focus: list[str]) -> None:
    """I4 — the three `collected_per='call'` leaves are unconditional."""
    missing = doc.collected_per_call_paths() - set(focus)
    assert not missing, f"call-scoped path(s) missing from focus: {len(missing)}"


def assert_invariants(
    doc: FormSchemaDoc, raw: dict[str, Any], scenario: Scenario, focus: list[str]
) -> None:
    """The four global retry-scope invariants every scenario in this suite must satisfy."""
    assert is_v2(raw)
    _assert_soundness(doc, scenario, focus)
    _assert_completeness(doc, scenario, focus)
    _assert_group_closure(doc, scenario, focus)
    _assert_call_scoped_always(doc, focus)


_FAMILY_SPOUSE_VALUES: dict[str, Any] = {
    "sections.benefit_coverage.coverage_type": "Family",
    "sections.patient_information.spouse_partner_name": "Test Spouse",
    "sections.patient_information.spouse_partner_dob": "1990-01-01",
}

_CALL_SCOPED_VALUES: dict[str, Any] = {
    "sections.patient_verification.is_insurance_active": "Yes",
    "sections.insurance_representative.rep_name": "Test Rep",
    "sections.insurance_representative.call_reference_number": "REF-0001",
}

_SPOUSE_PATHS = frozenset(
    {
        "sections.patient_information.spouse_partner_name",
        "sections.patient_information.spouse_partner_dob",
    }
)

_OFFICE_VISIT_GROUP = "sections.general_coverage.office_visits.cpt_99211"
_OFFICE_VISIT_CONFIRMED_VALUES: dict[str, Any] = {
    f"{_OFFICE_VISIT_GROUP}.covered": "Yes",
    f"{_OFFICE_VISIT_GROUP}.copay": "$25",
    f"{_OFFICE_VISIT_GROUP}.coinsurance": "10%",
}
_OFFICE_VISIT_PATHS = frozenset(
    {
        f"{_OFFICE_VISIT_GROUP}.covered",
        f"{_OFFICE_VISIT_GROUP}.copay",
        f"{_OFFICE_VISIT_GROUP}.coinsurance",
        f"{_OFFICE_VISIT_GROUP}.prior_auth",
    }
)


def test_nothing_on_file() -> None:
    """An empty form still owes only what its own gates allow — the baseline the mutation
    proofs run against."""
    scenario = Scenario("nothing on file", values={}, confirmed_by_authoritative=False)
    assert_invariants(_DOC, _RAW, scenario, _focus(scenario))


def test_family_plan_spouse_details_confirmed_by_authoritative_call() -> None:
    scenario = Scenario(
        "family plan, spouse details confirmed by an authoritative call",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _SPOUSE_PATHS.isdisjoint(focus)


def test_family_plan_spouse_details_from_a_non_authoritative_call() -> None:
    """A non-authoritative call is not proof, so the defaulted spouse leaves are still owed."""
    scenario = Scenario(
        "family plan, spouse details from a non-authoritative call",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _SPOUSE_PATHS


def test_family_plan_spouse_details_from_intake() -> None:
    """Spec D8's headline case: an intake value was never put to the payer, so a retry still
    owes it, even though `is_field_satisfied` would call it complete."""
    scenario = Scenario(
        "family plan, spouse details from intake",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario, intake=True)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _SPOUSE_PATHS


def test_one_unconfirmed_group_member_pulls_in_its_already_confirmed_siblings() -> None:
    """I3: `covered`/`copay`/`coinsurance` are confirmed, `prior_auth` is not — group closure
    must re-open the whole panel rather than asking `prior_auth` in isolation."""
    scenario = Scenario(
        "one CPT group member unconfirmed, its siblings already confirmed",
        values=dict(_OFFICE_VISIT_CONFIRMED_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _OFFICE_VISIT_PATHS


def test_call_scoped_fields_confirmed_by_authoritative_call_are_still_asked() -> None:
    """I4: the rep's name and the call reference number describe THIS call, so even a fully
    authoritative confirmation from an earlier call must not remove them (spec D8)."""
    scenario = Scenario(
        "call-scoped fields confirmed by an authoritative call",
        values=dict(_CALL_SCOPED_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _DOC.collected_per_call_paths() <= set(focus)
